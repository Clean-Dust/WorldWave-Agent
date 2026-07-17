# Wrong dict default key

`pkg/config.py::get_setting` looks up the wrong default key when missing. Should return `defaults[key]` if present else None. `pkg/app.py` uses it for theme.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
