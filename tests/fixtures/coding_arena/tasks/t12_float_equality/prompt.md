# Float equality without tolerance

`pkg/geo.py::near` should treat points within eps as equal but uses exact `==`. Fix with abs diff. Adversarial numeric edge.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
