import unittest

from solution import page_slice


class PaginationTests(unittest.TestCase):
    def test_middle_page(self):
        self.assertEqual(page_slice([1, 2, 3, 4], 1, 2), [2, 3])

    def test_negative_offset(self):
        with self.assertRaises(ValueError):
            page_slice([1, 2, 3], -1, 2)
