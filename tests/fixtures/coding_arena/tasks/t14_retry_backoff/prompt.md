# Retry backoff never increases

`pkg/retry.py::next_delay` should double delay each attempt (capped) but returns constant. Inspired by client SDK retry bugs.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
