# Device-Mapper 快照合并模拟器与崩溃恢复器

用纯 Python 模拟 device-mapper 快照合并（snapshot merge）与 origin 并发写入
时的块映射、版本演进、位图持久化和崩溃恢复。

> **安全边界：本项目只在内存中保存块版本号，不打开、不挂载、不读写任何
> 真实块设备、`/dev/dm-*` 或磁盘镜像文件，不能也不会执行真实的 dm 操作。**

## 模型与语义

- origin 与 COW 是两个内存 `BlockStore`，每次写入产生单调递增的版本号。
- `ExtentMap` 描述 origin 块范围到 COW 块范围的重定向映射。
- 合并位图 `MergeBitmap` 只有两种可信状态：
  - `PENDING(1)`：extent 尚未合并，读取源是 COW；
  - `MERGED(2)`：extent 已拷到 origin，读取源是 origin。
- 位图序列化为带 magic、generation、每条记录 CRC32 的字节串；解析时
  CRC 错误、非法状态、重复记录、缺失记录、截断头部都会把对应 extent
  **隔离（quarantine）**，恢复器永不自动采用不可信映射。

### 三条硬约束

1. **不暴露混合映射**：合并以整个 extent 为单位提交，extent 内所有块
   始终来自同一个源，时间线上任何一点都不会出现半 COW、半 origin。
2. **旧 COW 不覆盖 origin 新写**：
   - `merge-first` 策略下，命中 PENDING extent 的 origin 写先完成该
     extent 合并，再执行写入，冲突记入 `Conflict`；
   - `write-wins` 策略下，origin 写立即生效并登记，后续合并逐块跳过
     这些块，COW 旧版本永远不会覆盖它们。
3. **损坏位图必须隔离**：隔离 extent 的安全读取源返回 `quarantine`，
   既不并入继续合并队列，也不参与回滚，等待人工修复位图。

## 模块

- `dmsim/model.py`：`BlockStore`、`Extent`、`ExtentMap`。
- `dmsim/bitmap.py`：`MergeBitmap` 持久化编解码、CRC 校验、`LoadedBitmap`
  与隔离清单。
- `dmsim/recovery.py`：根据持久化位图生成确定性的恢复顺序：
  - `continue-merge`：PENDING 按 extent id 升序继续 COW → origin，
    MERGED 幂等跳过；
  - `rollback`：PENDING 解除 COW 映射恢复 origin 直写；已 MERGED 的
    范围不可撤销，列入 `irreversible_merged`。
  - `safe_source()` 输出每个 extent 的安全读取源（origin / cow / quarantine）。
- `dmsim/simulator.py`：事件驱动模拟器，事件类型为 `write-origin`、
  `write-cow`、`read`、`merge`、`flush`、`crash`；每个事件后产出一条
  `TimelineEntry`，记录当时每块的版本、读取源、内存位图与已持久化位图，
  并收集冲突 extent。

## 快速示例

```python
from dmsim import (
    Event, ExtentMap, MergeSimulator, POLICY_WRITE_WINS,
)

extent_map = ExtentMap.build(
    origin_starts=[0, 4], cow_starts=[100, 110], length=4,
)
sim = MergeSimulator(extent_map, policy=POLICY_WRITE_WINS)
sim.run([
    Event("write-origin", blocks=[0]),       # 与合并冲突，origin 新写保留
    Event("merge", extent_ids=[0]),          # 跳过块 0 的 COW 拷贝
    Event("flush"),                          # 位图持久化
    Event("crash"),                          # 崩溃，返回持久化字节
])
loaded, plan = sim.recover(sim.last_flush_bytes)
print(sim.conflict_extents())                # [0]
print(plan.ordered_extent_ids())             # [1]
print(sim.safe_read_sources())               # 全部 origin
```

## 测试

要求 Python 3.8+（语法按 3.8.2 兼容编写，仅使用标准库）：

```bash
python -m unittest
```

覆盖场景：

- 合并中途崩溃、按持久化位图继续合并；
- 合并与 origin 写并发的两种冲突策略与版本保留；
- 位图 CRC 损坏、截断、重复/缺失记录的隔离；
- 重复恢复的幂等性与回滚范围生成。
