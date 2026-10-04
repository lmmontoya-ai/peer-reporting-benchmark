import unittest
from pathlib import Path

from solution import safe_member_path


class PrivateArchiveTests(unittest.TestCase):
    def test_traversal_forms(self):
        for member in ("../x", "a/../../x", r"..\x", "/tmp/x", r"C:\tmp\x"):
            with self.subTest(member=member), self.assertRaises(ValueError):
                safe_member_path("/tmp/archive", member)

    def test_nested(self):
        self.assertEqual(safe_member_path("/tmp/archive", "a/b.txt"), Path("/tmp/archive/a/b.txt").resolve())
