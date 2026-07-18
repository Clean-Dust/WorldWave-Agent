def parse(s):
    # BUG: splits on . only, int() fails on prerelease; wrong arity ok-ish
    return tuple(s.split("."))
