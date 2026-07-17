from pkg.csvkit import parse_line
from pkg.loader import parse_rows

def test_simple():
    assert parse_line("a,b,c") == ["a", "b", "c"]

def test_quoted():
    assert parse_line('a,"b,c",d') == ["a", "b,c", "d"]

def test_rows():
    rows = parse_rows('x,y\n"1,2",3')
    assert rows[0] == ["x", "y"]
    assert rows[1] == ["1,2", "3"]
