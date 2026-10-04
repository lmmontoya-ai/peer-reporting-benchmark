import unittest

from solution import merge_headers


class PrivateHeaderTests(unittest.TestCase):
    def test_multiple_case_variants(self):
        original = {"ETag": "old", "Accept": "json"}
        updates = {"etag": "new", "X-ID": "7"}
        self.assertEqual(merge_headers(original, updates), {"ETag": "new", "Accept": "json", "X-ID": "7"})
        self.assertEqual(original["ETag"], "old")
        self.assertEqual(updates["etag"], "new")

    def test_empty(self):
        self.assertEqual(merge_headers({}, {}), {})
