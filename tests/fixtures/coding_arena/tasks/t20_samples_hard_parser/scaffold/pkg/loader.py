from .csvkit import parse_line

def parse_rows(text):
    return [parse_line(L) for L in text.splitlines() if L.strip()]
