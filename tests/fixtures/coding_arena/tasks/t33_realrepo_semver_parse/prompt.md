# Parse simple semver (realrepo)

`pkg/semver.py::parse` parses `MAJOR.MINOR.PATCH` into a tuple of ints; ignore pre-release suffix after `-`. `pkg/compat.py` uses parse. Tagged realrepo.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
