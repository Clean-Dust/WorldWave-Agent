from .pricing import apply_discount

def line_total(price, pct):
    return apply_discount(price, pct)
