# Always-wrong echo (circuit metrics path)

`pkg/echo.py::transform` should uppercase text but lowercases. Multi-file. Used to exercise circuit/replan metrics when partial fixes fail.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
