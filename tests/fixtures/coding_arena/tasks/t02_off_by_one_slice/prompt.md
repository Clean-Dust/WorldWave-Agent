# Off-by-one in window slice

`pkg/window.py::take_first_n` should return the first n items but drops the last. Fix the slice. `pkg/api.py` depends on it.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
