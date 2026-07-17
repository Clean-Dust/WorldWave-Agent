from pkg.rank import top_scores
from pkg.leaderboard import format_board

def test_top():
    data = [("a", 1), ("b", 9), ("c", 5)]
    assert top_scores(data, 2) == [("b", 9), ("c", 5)]

def test_format():
    assert format_board([("x", 2), ("y", 8)], 1) == ["y:8"]
