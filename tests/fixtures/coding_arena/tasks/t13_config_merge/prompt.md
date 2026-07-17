# Shallow config merge loses nested

`pkg/merge.py::deep_merge` currently overwrites nested dicts entirely. Must deep-merge dict values. Multi-file: used by `pkg/settings.py`.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
