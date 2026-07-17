from .echo import transform

def run(items):
    return [transform(x) for x in items]
