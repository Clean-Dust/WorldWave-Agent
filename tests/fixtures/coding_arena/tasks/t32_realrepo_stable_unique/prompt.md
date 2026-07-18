# Stable unique preserving order (realrepo)

`pkg/unique.py::stable_unique` returns unique items preserving first-seen order. `pkg/batch.py` uses it. Tagged realrepo.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
