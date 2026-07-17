def parse_line(line):
    # BUG: naive split ignores quotes
    return (line or "").split(",")
