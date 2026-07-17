# Naive UTC assume broken

`pkg/timeutil.py::to_epoch` treats naive datetimes as local by using wrong assumption — should treat naive as UTC. Multi-file with `pkg/events.py`.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
