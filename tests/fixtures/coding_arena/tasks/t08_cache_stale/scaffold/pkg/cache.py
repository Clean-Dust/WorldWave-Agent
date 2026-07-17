class Store:
    def __init__(self):
        self._data = {}
        self._version = 0
        self._snapshot = {}  # BUG: get uses snapshot never refreshed

    def set(self, key, value):
        self._data[key] = value
        self._version += 1
        return self._version

    def get(self, key, default=None):
        # BUG: reads snapshot instead of live data
        if key in self._snapshot:
            return self._snapshot[key]
        return default
