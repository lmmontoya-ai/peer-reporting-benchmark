def parse_duration(text):
    """Parse a duration in milliseconds."""
    value = text.strip()
    if value.endswith("ms"):
        return int(value[:-2])
    if value.endswith("s"):
        return int(value[:-1]) * 1000
    if value.endswith("m"):
        return int(value[:-1]) * 60000
    raise ValueError("unsupported duration")
