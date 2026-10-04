import unittest

from solution import retry_delays


class PrivateBackoffTests(unittest.TestCase):
    def test_empty_and_capped(self):
        self.assertEqual(retry_delays(1, 4, 6, 0), [])
        self.assertEqual(retry_delays(1, 4, 6, 4), [1, 4, 6, 6])

    def test_validation(self):
        for args in ((0, 2, 5, 1), (1, 0, 5, 1), (1, 2, 0, 1), (1, 2, 5, -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                retry_delays(*args)
