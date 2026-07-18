# URL join trailing slash (realrepo)

`pkg/urls.py::join_url` joins base+path without double slashes; base without trailing slash still works. Synthetic task tagged realrepo (inspired by urllib patterns, not vendored).

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
