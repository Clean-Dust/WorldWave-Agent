# Deep merge JSON configs (samples path)

`pkg/jmerge.py::merge_json` deep-merges dicts (override wins leaf); lists are replaced not concatenated. Multi-file with `pkg/loader.py`. Hard samples path.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
