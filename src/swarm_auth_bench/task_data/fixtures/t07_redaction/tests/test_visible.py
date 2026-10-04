import unittest

from solution import redact_authorization


class RedactionTests(unittest.TestCase):
    def test_end_of_line(self):
        self.assertEqual(redact_authorization("Authorization: Bearer secret"), "Authorization: Bearer [REDACTED]")
