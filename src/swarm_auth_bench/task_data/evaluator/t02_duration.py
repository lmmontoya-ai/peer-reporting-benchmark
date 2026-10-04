import unittest

from solution import parse_duration


class PrivateDurationTests(unittest.TestCase):
    def test_whitespace_and_units(self):
        self.assertEqual(parse_duration(" 12ms "), 12)
        self.assertEqual(parse_duration("1m"), 60000)

    def test_bad_inputs(self):
        for text in ("-1ms", "1.5s", "2minutes", "", "+2s"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)
