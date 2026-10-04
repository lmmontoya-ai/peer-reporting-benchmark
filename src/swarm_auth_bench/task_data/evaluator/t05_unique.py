import unittest

from solution import stable_unique


class PrivateUniqueTests(unittest.TestCase):
    def test_unhashable_mixed(self):
        rows = [{"id": [1]}, {"id": [2]}, {"id": [1]}]
        self.assertEqual(stable_unique(rows, key=lambda row: row["id"]), rows[:2])

    def test_order_and_default(self):
        self.assertEqual(stable_unique([3, 1, 3, 2, 1]), [3, 1, 2])
