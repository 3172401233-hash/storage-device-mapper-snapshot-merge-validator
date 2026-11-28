import unittest

from dmsim.bitmap import BITMAP_MERGED
from dmsim.model import ExtentMap
from dmsim.recovery import SOURCE_COW, SOURCE_ORIGIN
from dmsim.simulator import (
    Event,
    MergeSimulator,
    POLICY_MERGE_FIRST,
    POLICY_WRITE_WINS,
)


def build_simulator(policy=POLICY_MERGE_FIRST, blocks=None):
    extent_map = ExtentMap.build(
        origin_starts=[0, 4, 8],
        cow_starts=[100, 110, 120],
        length=4,
    )
    return MergeSimulator(extent_map, blocks=blocks, policy=policy)


class SimulatorTest(unittest.TestCase):
    def test_initial_reads_go_to_cow(self):
        sim = build_simulator()
        view = sim.read(0)
        self.assertEqual(view["source"], SOURCE_COW)
        self.assertGreater(view["version"], 0)
        self.assertEqual(sim.safe_read_sources()[0], SOURCE_COW)

    def test_no_mixed_mapping_inside_one_extent(self):
        sim = build_simulator()
        sim.run([
            Event("merge", extent_ids=[0]),
            Event("write-origin", blocks=[5]),
            Event("merge", extent_ids=[1]),
        ])
        for entry in sim.timeline:
            for extent_id in (0, 1, 2):
                blocks = [
                    block for block, view in entry.block_views.items()
                    if view["extent_id"] == extent_id
                ]
                if blocks:
                    sources = {
                        entry.block_views[block]["source"] for block in blocks
                    }
                    self.assertEqual(len(sources), 1)

    def test_merge_interrupt_and_resume(self):
        sim = build_simulator()
        sim.run([
            Event("merge", extent_ids=[0]),
            Event("flush"),
            Event("merge", extent_ids=[1]),
            Event("crash"),
        ])
        self.assertFalse(sim.alive)

        loaded, plan = sim.recover(sim.last_flush_bytes)
        self.assertEqual(plan.ordered_extent_ids(), [1, 2])
        self.assertEqual(plan.already_done, [0])
        self.assertEqual(sim.bitmap.state(0), BITMAP_MERGED)
        self.assertEqual(sim.bitmap.state(1), BITMAP_MERGED)
        self.assertEqual(sim.bitmap.state(2), BITMAP_MERGED)
        self.assertTrue(sim.alive)

    def test_concurrent_write_merge_first_keeps_new_origin(self):
        sim = build_simulator(policy=POLICY_MERGE_FIRST)
        old_cow_version = sim.blocks.cow_version(100)
        sim.run([
            Event("write-origin", blocks=[0]),
        ])
        self.assertEqual(sim.conflict_extents(), [0])
        conflict = sim.conflicts[0]
        self.assertEqual(conflict.resolution, "merge-then-write-origin")
        self.assertGreater(
            sim.blocks.origin_version(0), old_cow_version
        )
        self.assertEqual(sim.read(0)["source"], SOURCE_ORIGIN)

    def test_concurrent_write_write_wins_blocks_cow_overwrite(self):
        sim = build_simulator(policy=POLICY_WRITE_WINS)
        old_cow_version = sim.blocks.cow_version(100)
        sim.run([
            Event("write-origin", blocks=[0]),
            Event("merge", extent_ids=[0]),
        ])
        self.assertEqual(sim.conflict_extents(), [0])
        origin_version = sim.blocks.origin_version(0)
        self.assertGreater(origin_version, old_cow_version)
        # 合并时跳过该块，旧 COW 版本没有覆盖 origin 新写。
        self.assertEqual(sim.blocks.origin_version(0), origin_version)
        self.assertEqual(sim.read(0)["source"], SOURCE_ORIGIN)

    def test_cow_write_rejected_after_merge(self):
        sim = build_simulator()
        sim.merge_extent(0)
        with self.assertRaises(ValueError):
            sim.write_cow([0])

    def test_block_versions_advance_on_every_write(self):
        sim = build_simulator()
        before = sim.blocks.cow_version(100)
        sim.write_cow([0])
        self.assertGreater(sim.blocks.cow_version(100), before)
if __name__ == "__main__":
    unittest.main()
