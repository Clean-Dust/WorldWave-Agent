from .urls import join_url

def endpoint(base, name):
    return join_url(base, name)
