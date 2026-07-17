from .timeutil import to_epoch

def event_stamp(dt):
    return {"ts": to_epoch(dt)}
