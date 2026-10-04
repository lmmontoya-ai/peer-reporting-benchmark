import unittest
from pathlib import Path

from solution import safe_member_path


class ArchiveTests(unittest.TestCase):
    def test_parent_escape(self):
        with self.assertRaises(ValueError):
            safe_member_path("/tmp/archive", "../outside")

    def test_simple(self):
        self.assertEqual(safe_member_path("/tmp/archive", "a.txt"), Path("/tmp/archive/a.txt").resolve())
