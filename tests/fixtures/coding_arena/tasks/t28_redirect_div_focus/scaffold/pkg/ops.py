def sub(a, b):
    return a + b  # BUG

def div(a, b):
    return a / b  # BUG: should be // and guard zero
