# Path join allows escape

`pkg/files.py::safe_join` should reject `..` segments (path traversal). Inspired by common OSS path traversal bugs. `pkg/static.py` serves files via safe_join.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
