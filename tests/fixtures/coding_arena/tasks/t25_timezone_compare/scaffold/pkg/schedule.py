def is_due(now, deadline):
    # BUG: compares naive/aware incorrectly (may TypeError or wrong)
    return now >= deadline
