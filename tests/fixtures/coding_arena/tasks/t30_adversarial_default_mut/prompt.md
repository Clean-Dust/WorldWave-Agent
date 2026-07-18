# Default arg mutation trap

`pkg/bag.py::add_item` must not share a mutable default list across calls. `pkg/store.py` wraps it. Adversarial edge case.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
