# Pagination cursor exclusive/inclusive

`pkg/page.py::slice_after` should return items after cursor (exclusive) but includes the cursor item. Fix exclusive semantics.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
