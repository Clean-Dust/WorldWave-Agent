class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email

    def display(self):
        return self.name  # BUG: no strip

    def normalized_email(self):
        return self.email  # BUG: no lower/strip
