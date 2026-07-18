# Path resolve must block abs + ..

`pkg/paths.py::resolve_under` must keep results under root: reject absolute second parts and `..` segments. `pkg/assets.py` uses it.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
