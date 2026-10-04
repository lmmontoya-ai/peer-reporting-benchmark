import re


def redact_authorization(line):
    """Redact a bearer token in a log line."""
    return re.sub(r"Authorization: Bearer (\S+)\s", "Authorization: Bearer [REDACTED] ", line)
