# JSON response key typo

`pkg/api_client.py::parse_user` reads `user_name` but payloads use `username`. Fix the key. `pkg/session.py` stores the result.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
