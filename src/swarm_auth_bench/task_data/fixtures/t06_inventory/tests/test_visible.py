import unittest

from solution import apply_stock


class InventoryTests(unittest.TestCase):
    def test_copy(self):
        original = {"bolt": 2}
        self.assertEqual(apply_stock(original, [("bolt", 1)]), {"bolt": 3})
        self.assertEqual(original, {"bolt": 2})

    def test_negative(self):
        with self.assertRaises(ValueError):
            apply_stock({"bolt": 1}, [("bolt", -2)])
