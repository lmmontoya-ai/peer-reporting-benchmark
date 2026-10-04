import unittest

from solution import stable_unique


class UniqueTests(unittest.TestCase):
    def test_unhashable_keys(self):
        self.assertEqual(stable_unique([1, 2, 1], key=lambda number: [number]), [1, 2])
