from .cache import Store

class UserRepo:
    def __init__(self):
        self.store = Store()

    def put(self, uid, name):
        self.store.set(uid, name)

    def get(self, uid):
        return self.store.get(uid)
