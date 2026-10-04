def stable_unique(values, key=lambda item: item):
    """Keep the first value for each key."""
    seen = set()
    result = []
    for value in values:
        marker = key(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
