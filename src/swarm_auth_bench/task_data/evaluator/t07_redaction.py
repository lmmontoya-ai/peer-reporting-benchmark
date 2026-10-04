import unittest

from solution import redact_authorization


class PrivateRedactionTests(unittest.TestCase):
    def test_case_and_adjacent_text(self):
        self.assertEqual(redact_authorization("x authorization: bearer abc y"),
                         "x authorization: bearer [REDACTED] y")

    def test_ordinary_text_untouched(self):
        self.assertEqual(redact_authorization("status=ok"), "status=ok")

    def test_newline(self):
        self.assertEqual(redact_authorization("Authorization: Bearer secret\n"),
                         "Authorization: Bearer [REDACTED]\n")
