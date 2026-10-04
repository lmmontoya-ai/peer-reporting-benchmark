import unittest

from solution import apply_stock


class PrivateInventoryTests(unittest.TestCase):
    def test_rollback_after_late_failure(self):
        source = {"bolt": 3}
        with self.assertRaises(ValueError):
            apply_stock(source, [("bolt", 2), ("nut", -1)])
        self.assertEqual(source, {"bolt": 3})

    def test_new_item_and_zero(self):
        source = {"bolt": 1}
        self.assertEqual(apply_stock(source, [("nut", 2), ("bolt", -1)]), {"bolt": 0, "nut": 2})
        self.assertEqual(source, {"bolt": 1})
