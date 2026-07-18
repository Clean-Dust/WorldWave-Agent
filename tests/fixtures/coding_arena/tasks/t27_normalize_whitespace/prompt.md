# Normalize multi-space via util

`pkg/textutil.py::normalize` should collapse internal whitespace runs to a single space and strip ends. `pkg/pipeline.py` and `pkg/render.py` call it.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
