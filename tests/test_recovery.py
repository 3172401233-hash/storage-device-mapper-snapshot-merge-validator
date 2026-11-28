import unittest

from dmsim.bitmap import (
    BITMAP_MERGED,
    BITMAP_PENDING,
    MergeBitmap,
)
from dmsim.model import BlockStore, ExtentMap
from dmsim.recovery import (
    ACTION_CONTINUE,
    ACTION_ROLLBACK,
    SOURCE_COW,
    SOURCE_ORIGIN,
    SOURCE_QUARANTINE,
    apply_recovery_plan,
    build_recovery_plan,
    safe_source,
)
from dmsim.simulator import Event, MergeSimulator


def build_map():
    return ExtentMap.build(
        origin_starts=[0, 4, 8],
        cow_starts=[100, 110, 120],
        length=4,
    )


class RecoveryPlannerTest(unittest.TestCase):
    def setUp(self):
        self.extent_map = build_map()
        self.bitmap = MergeBitmap()
        self.bitmap.mark(0, BITMAP_MERGED)
        self.bitmap.mark(1, BITMAP_PENDING)
        self.bitmap.mark(2, BITMAP_PENDING)
        self.blocks = BlockStore()
        for cow_block in (100, 101, 102, 103, 110, 111, 112, 113,
                          120, 121, 122, 123):
            self.blocks.write_cow(cow_block, 1)

    def test_continue_plan_order_and_sources(self):
        loaded = MergeBitmap.from_bytes(
            self.bitmap.to_bytes(), self.extent_map.ids()
        )
        plan = build_recovery_plan(loaded, self.extent_map, ACTION_CONTINUE)
        self.assertEqual(plan.ordered_extent_ids(), [1, 2])
        self.assertEqual(plan.already_done, [0])
        self.assertEqual(plan.ranges[0].from_store, SOURCE_COW)
        self.assertEqual(plan.ranges[0].to_store, SOURCE_ORIGIN)
        self.assertEqual(safe_source(0, loaded), SOURCE_ORIGIN)
        self.assertEqual(safe_source(1, loaded), SOURCE_COW)

    def test_apply_continue_copies_cow_versions_to_origin(self):
        loaded = MergeBitmap.from_bytes(
            self.bitmap.to_bytes(), self.extent_map.ids()
        )
        plan = build_recovery_plan(loaded, self.extent_map, ACTION_CONTINUE)
        apply_recovery_plan(
            plan, loaded, self.extent_map, self.blocks, self.bitmap
        )
        self.assertEqual(len(plan.performed), 2)
        for extent_id in (0, 1, 2):
            self.assertEqual(self.bitmap.state(extent_id), BITMAP_MERGED)
        self.assertGreater(self.blocks.origin_version(4), 0)
        self.assertGreater(self.blocks.origin_version(8), 0)

    def test_repeated_recovery_is_idempotent(self):
        raw = self.bitmap.to_bytes()

        first = MergeBitmap.from_bytes(raw, self.extent_map.ids())
        first_plan = build_recovery_plan(first, self.extent_map)
        apply_recovery_plan(first_plan, first, self.extent_map,
                            self.blocks, self.bitmap)
        version_after_first = self.blocks.origin_version(4)

        second = MergeBitmap.from_bytes(self.bitmap.to_bytes(),
                                        self.extent_map.ids())
        second_plan = build_recovery_plan(second, self.extent_map)
        apply_recovery_plan(second_plan, second, self.extent_map,
                            self.blocks, self.bitmap)
        self.assertEqual(second_plan.ranges, [])
        self.assertEqual(second_plan.already_done, [0, 1, 2])
        self.assertEqual(
            self.blocks.origin_version(4), version_after_first
        )

    def test_rollback_drops_pending_cow_mapping(self):
        loaded = MergeBitmap.from_bytes(
            self.bitmap.to_bytes(), self.extent_map.ids()
        )
        plan = build_recovery_plan(loaded, self.extent_map, ACTION_ROLLBACK)
        self.assertEqual(plan.ordered_extent_ids(), [1, 2])
        self.assertEqual(plan.irreversible_merged, [0])
        self.assertEqual(plan.ranges[0].action, ACTION_ROLLBACK)
        apply_recovery_plan(plan, loaded, self.extent_map,
                            self.blocks, self.bitmap)
        self.assertIsNone(self.bitmap.state(1))
        self.assertEqual(self.bitmap.state(0), BITMAP_MERGED)

    def test_corrupt_bitmap_extents_are_quarantined(self):
        raw = bytearray(self.bitmap.to_bytes())
        raw[16 + 5] ^= 0xFF
        loaded = MergeBitmap.from_bytes(bytes(raw), self.extent_map.ids())
        self.assertTrue(loaded.is_isolated(0))
        self.assertEqual(safe_source(0, loaded), SOURCE_QUARANTINE)

        plan = build_recovery_plan(loaded, self.extent_map)
        self.assertNotIn(0, plan.ordered_extent_ids())
        self.assertIn(0, plan.isolated)
        apply_recovery_plan(plan, loaded, self.extent_map,
                            self.blocks, self.bitmap)
        # 隔离 extent 不允许恢复器触碰：位图初始状态是 MERGED 时保持原状，
        # 但它只出现在隔离清单中，不会进入任何执行范围。
        self.assertNotIn(0, plan.ordered_extent_ids())
        self.assertTrue(loaded.is_isolated(0))


class SimulatorRecoveryTest(unittest.TestCase):
    def test_end_to_end_crash_continue_and_reread(self):
        sim = MergeSimulator(build_map())
        sim.run([
            Event("merge", extent_ids=[0]),
            Event("flush"),
            Event("merge", extent_ids=[1]),
            Event("crash"),
        ])
        raw = sim.last_flush_bytes
        loaded, plan = sim.recover(raw)
        self.assertEqual(plan.ordered_extent_ids(), [1, 2])
        self.assertEqual(sim.safe_read_sources()[0], SOURCE_ORIGIN)
        self.assertEqual(sim.safe_read_sources()[8], SOURCE_ORIGIN)

        loaded_again, plan_again = sim.recover(sim.bitmap.to_bytes())
        self.assertEqual(plan_again.ranges, [])
        self.assertTrue(loaded_again.healthy)


if __name__ == "__main__":
    unittest.main()
