from .pricing import apply_discount

def total(price, pct):
    return apply_discount(price, pct)
