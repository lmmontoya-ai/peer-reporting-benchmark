import unittest

from solution import merge_headers


class HeaderTests(unittest.TestCase):
    def test_update_case(self):
        self.assertEqual(merge_headers({"Content-Type": "a"}, {"content-type": "b"}), {"Content-Type": "b"})
