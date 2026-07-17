# None guard on normalize

`pkg/textutil.py::normalize_name` crashes on None. It should return empty string for None/empty and lowercase-strip otherwise. Used by `pkg/users.py`.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
