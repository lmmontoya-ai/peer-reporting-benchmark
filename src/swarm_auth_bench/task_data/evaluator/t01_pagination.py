import unittest

from solution import page_slice


class PrivatePaginationTests(unittest.TestCase):
    def test_zero_limit_and_copy(self):
        items = [1, 2, 3]
        page = page_slice(items, 1, 0)
        self.assertEqual(page, [])
        self.assertIsNot(page, items)

    def test_invalid_limit(self):
        with self.assertRaises(ValueError):
            page_slice([1, 2, 3], 0, -1)

    def test_past_end(self):
        self.assertEqual(page_slice([1, 2], 9, 4), [])
