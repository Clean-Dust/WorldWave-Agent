def top_scores(pairs, k=3):
    """pairs: list of (name, score). Return top-k by score desc."""
    ordered = sorted(pairs, key=lambda p: p[1], reverse=False)  # BUG
    return ordered[:k]
