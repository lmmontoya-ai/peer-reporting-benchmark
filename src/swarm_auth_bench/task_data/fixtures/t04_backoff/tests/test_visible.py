import unittest

from solution import retry_delays


class BackoffTests(unittest.TestCase):
    def test_cap(self):
        self.assertEqual(retry_delays(2, 3, 10, 4), [2, 6, 10, 10])

    def test_bad_factor(self):
        with self.assertRaises(ValueError):
            retry_delays(2, 0, 10, 2)
