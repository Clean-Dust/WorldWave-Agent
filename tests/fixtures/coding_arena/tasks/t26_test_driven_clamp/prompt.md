# Clamp score with multi-file

`pkg/score.py::clamp` must return values in [0, 100]. `pkg/report.py` formats clamped scores. Agent-visible stub tests under scaffold tests/ drive TDD.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
