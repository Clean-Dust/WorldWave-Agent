# Fix broken add operator

`pkg/math_ops.py::add` returns incorrect results for positive integers. Fix `add` so `add(a,b)==a+b`. `pkg/service.py` calls add — keep the public API stable.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
