from .rank import top_scores

def format_board(pairs, k=3):
    return [f"{n}:{s}" for n, s in top_scores(pairs, k)]
