# Multi-file discount stacking

`pkg/pricing.py::apply_discount` should apply percent discount correctly: `price * (1 - pct/100)`. Callers in `pkg/cart.py` and `pkg/checkout.py` must keep working. Multi-file refactor-style locate.
