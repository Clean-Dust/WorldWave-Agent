# Rate limit inclusive bug

`pkg/ratelimit.py::allow` should allow at most `limit` calls per window but uses `>` instead of `>=` incorrectly (off-by-one: allows limit+1). Inspired by API gateway rate-limit edge cases.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
