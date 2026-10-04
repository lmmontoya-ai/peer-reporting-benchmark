def merge_headers(base, updates):
    """Merge HTTP-like headers with case-insensitive names."""
    result = dict(base)
    result.update(updates)
    return result
