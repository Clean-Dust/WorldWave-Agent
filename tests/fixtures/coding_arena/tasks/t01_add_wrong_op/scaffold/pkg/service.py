"""Service layer over math_ops."""
from .math_ops import add, mul

def compute(a, b, op="add"):
    if op == "add":
        return add(a, b)
    if op == "mul":
        return mul(a, b)
    raise ValueError(op)
