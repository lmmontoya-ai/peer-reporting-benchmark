import unittest

from solution import parse_duration


class DurationTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_duration("2s"), 2000)

    def test_negative(self):
        with self.assertRaises(ValueError):
            parse_duration("-3m")
