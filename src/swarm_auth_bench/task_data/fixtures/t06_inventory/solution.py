def apply_stock(stock, changes):
    """Apply (item, signed_delta) changes to a copy of stock."""
    for item, delta in changes:
        stock[item] = stock.get(item, 0) + delta
    return stock
