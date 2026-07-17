# Token expiry uses wrong comparison

`pkg/auth.py::is_expired` should treat `now >= exp` as expired but uses `>`. Inspired by JWT exp edge cases.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
