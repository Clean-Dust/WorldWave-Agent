# Unicode whitespace not stripped

`pkg/clean.py::clean_token` only strips ASCII spaces; must also strip unicode whitespace (\u00a0, \u2003). Adversarial edge case.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
