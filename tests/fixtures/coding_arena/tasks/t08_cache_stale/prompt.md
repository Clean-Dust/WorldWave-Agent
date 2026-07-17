# Stale cache after update

`pkg/cache.py::Store` returns stale values after `set` because get does not check version. Fix `get` to honor updates. `pkg/repo.py` wraps Store.

## Constraints
- Edit only files under the project scaffold.
- Do not invent secrets or network calls.
- Prefer minimal correct fixes; run tests if available.
