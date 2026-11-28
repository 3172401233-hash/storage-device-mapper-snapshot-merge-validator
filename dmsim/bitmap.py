"""合并进度位图的持久化编解码，含 CRC32 校验与损坏隔离。"""

import binascii
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

BITMAP_PENDING = 1
BITMAP_MERGED = 2
_VALID_STATES = (BITMAP_PENDING, BITMAP_MERGED)

_MAGIC = b"DMCOWBMP"
_VERSION = 1
_HEADER = struct.Struct(">8sBBHI")
_RECORD = struct.Struct(">IIBI")


class BitmapError(ValueError):
    """位图本身结构损坏（头错误或记录被截断）。"""


@dataclass
class IsolatedExtent:
    """被隔离的 extent 及原因，恢复器必须跳过它。"""

    extent_id: int
    reason: str


@dataclass
class LoadedBitmap:
    """位图加载结果：可信状态、隔离清单和完整性标志。"""

    states: Dict[int, int] = field(default_factory=dict)
    isolated: List[IsolatedExtent] = field(default_factory=list)
    header_error: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return self.header_error is None and not self.isolated

    def is_isolated(self, extent_id: int) -> bool:
        return any(item.extent_id == extent_id for item in self.isolated)


class MergeBitmap:
    """合并进度位图，PENDING 表示仍走 COW，MERGED 表示已落到 origin。"""

    def __init__(self, generation: int = 0) -> None:
        self.generation = generation
        self._states: Dict[int, int] = {}

    def mark(self, extent_id: int, state: int) -> None:
        if state not in _VALID_STATES:
            raise ValueError("未知位图状态 %d" % state)
        self._states[extent_id] = state

    def state(self, extent_id: int) -> Optional[int]:
        return self._states.get(extent_id)

    def remove(self, extent_id: int) -> None:
        self._states.pop(extent_id, None)

    def states_copy(self) -> Dict[int, int]:
        return dict(self._states)

    def bump_generation(self) -> int:
        self.generation += 1
        return self.generation

    def to_bytes(self) -> bytes:
        payload = bytearray()
        payload.extend(
            _HEADER.pack(_MAGIC, _VERSION, 0, 0, len(self._states))
        )
        for extent_id in sorted(self._states):
            state = self._states[extent_id]
            record_core = struct.pack(">IIB", extent_id, state, self.generation)
            checksum = binascii.crc32(record_core) & 0xFFFFFFFF
            payload.extend(_RECORD.pack(extent_id, state, self.generation, checksum))
        return bytes(payload)

    @staticmethod
    def from_bytes(data: bytes,
                   expected_extent_ids: Optional[List[int]] = None
                   ) -> LoadedBitmap:
        """解析位图，任何不可信的 extent 都进入隔离列表而不是静默采用。"""
        result = LoadedBitmap()
        expected = [] if expected_extent_ids is None else list(expected_extent_ids)

        if len(data) < _HEADER.size:
            result.header_error = "header-truncated"
            for extent_id in expected:
                result.isolated.append(IsolatedExtent(extent_id, "header-truncated"))
            return result

        magic, version, _flags, _reserved, count = _HEADER.unpack(
            data[:_HEADER.size]
        )
        if magic != _MAGIC or version != _VERSION:
            result.header_error = "bad-magic-or-version"
            for extent_id in expected:
                result.isolated.append(
                    IsolatedExtent(extent_id, "bad-magic-or-version")
                )
            return result

        body = data[_HEADER.size:]
        seen: Dict[int, int] = {}
        duplicates: List[int] = []

        for index in range(count):
            offset = index * _RECORD.size
            chunk = body[offset:offset + _RECORD.size]
            if len(chunk) < _RECORD.size:
                already = set(seen) | set(duplicates)
                for extent_id in expected:
                    if extent_id not in already:
                        result.isolated.append(
                            IsolatedExtent(extent_id, "truncated-records")
                        )
                result.header_error = "truncated-records"
                result.states = seen
                return result
            extent_id, state, _generation, checksum = _RECORD.unpack(chunk)
            record_core = chunk[:_RECORD.size - 4]
            actual = binascii.crc32(record_core) & 0xFFFFFFFF
            if actual != checksum:
                result.isolated.append(IsolatedExtent(extent_id, "crc-mismatch"))
                continue
            if state not in _VALID_STATES:
                result.isolated.append(IsolatedExtent(extent_id, "bad-state"))
                continue
            if extent_id in seen:
                duplicates.append(extent_id)
                continue
            seen[extent_id] = state

        for extent_id in duplicates:
            result.isolated.append(IsolatedExtent(extent_id, "duplicate-record"))
            seen.pop(extent_id, None)

        known = set(seen) | {item.extent_id for item in result.isolated}
        for extent_id in expected:
            if extent_id not in known:
                result.isolated.append(IsolatedExtent(extent_id, "missing-record"))

        result.states = seen
        return result
