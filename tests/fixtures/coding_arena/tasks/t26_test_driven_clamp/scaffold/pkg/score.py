def clamp(x):
    # BUG: only handles high side, ignores low/None
    if x > 100:
        return 100
    return x
