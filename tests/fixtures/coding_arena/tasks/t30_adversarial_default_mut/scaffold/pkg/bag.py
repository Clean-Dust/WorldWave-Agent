def add_item(item, bag=[]):
    # BUG: mutable default
    bag.append(item)
    return bag
