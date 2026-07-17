from .stats import mean

def avg_or_none(xs):
    try:
        return mean(xs)
    except ValueError:
        return None
