def page_slice(items, offset, limit):
    """Return the requested page as a separate list."""
    return items[offset:offset + limit]
