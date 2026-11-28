import unittest

from dmsim.bitmap import (
    BITMAP_MERGED,
    BITMAP_PENDING,
    MergeBitmap,
)


class BitmapCodecTest(unittest.TestCase):
    def setUp(self):
        self.ids = [0, 1, 2]
        self.bitmap = MergeBitmap(generation=3)
        self.bitmap.mark(0, BITMAP_MERGED)
        self.bitmap.mark(1, BITMAP_PENDING)
        self.bitmap.mark(2, BITMAP_PENDING)

    def test_roundtrip_preserves_states(self):
        raw = self.bitmap.to_bytes()
        loaded = MergeBitmap.from_bytes(raw, self.ids)
        self.assertTrue(loaded.healthy)
        self.assertEqual(loaded.states, self.bitmap.states_copy())
        self.assertEqual(loaded.isolated, [])

    def test_crc_corruption_isolates_extent(self):
        raw = bytearray(self.bitmap.to_bytes())
        header_size = 16
        raw[header_size + 5] ^= 0xFF
        loaded = MergeBitmap.from_bytes(bytes(raw), self.ids)
        self.assertFalse(loaded.healthy)
        self.assertEqual([item.extent_id for item in loaded.isolated], [0])
        self.assertEqual(loaded.isolated[0].reason, "crc-mismatch")
        self.assertNotIn(0, loaded.states)
        self.assertEqual(loaded.states[1], BITMAP_PENDING)

    def test_truncated_header_quarantines_all(self):
        loaded = MergeBitmap.from_bytes(b"short", self.ids)
        self.assertFalse(loaded.healthy)
        self.assertEqual(
            sorted(item.extent_id for item in loaded.isolated),
            self.ids,
        )

    def test_missing_record_is_isolated(self):
        raw = self.bitmap.to_bytes()
        loaded = MergeBitmap.from_bytes(raw, [0, 1, 2, 9])
        isolated_ids = [item.extent_id for item in loaded.isolated]
        self.assertIn(9, isolated_ids)
        self.assertEqual(loaded.states[9] if 9 in loaded.states else None, None)

    def test_duplicate_record_is_isolated(self):
        raw = bytearray(self.bitmap.to_bytes())
        record_size = 13
        body = bytes(raw[16:16 + record_size])
        raw.extend(body)
        raw[12:16] = (4).to_bytes(4, "big")
        loaded = MergeBitmap.from_bytes(bytes(raw), self.ids)
        self.assertFalse(loaded.healthy)
        self.assertTrue(
            any(item.reason == "duplicate-record" for item in loaded.isolated)
        )


if __name__ == "__main__":
    unittest.main()
