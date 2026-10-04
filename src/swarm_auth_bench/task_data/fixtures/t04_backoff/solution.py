def retry_delays(base, factor, cap, attempts):
    """Return one capped delay per attempt."""
    return [min(cap, base * factor ** index) for index in range(attempts)]
