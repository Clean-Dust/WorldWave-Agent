from .retry import next_delay

def delays(n, base=0.1, cap=2.0):
    return [next_delay(i, base, cap) for i in range(n)]
