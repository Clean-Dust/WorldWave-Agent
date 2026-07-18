# Aware/naive datetime compare

`pkg/schedule.py::is_due` should treat naive `now` as UTC when comparing to aware `deadline`. Multi-file with `pkg/jobs.py`.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
