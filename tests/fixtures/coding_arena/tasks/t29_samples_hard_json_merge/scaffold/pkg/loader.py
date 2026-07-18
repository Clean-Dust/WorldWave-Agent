from .jmerge import merge_json

def load_pair(a, b):
    return merge_json(a, b)
