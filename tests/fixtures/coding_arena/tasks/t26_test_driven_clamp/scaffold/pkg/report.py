from .score import clamp

def fmt(x):
    return f"score={clamp(x)}"
