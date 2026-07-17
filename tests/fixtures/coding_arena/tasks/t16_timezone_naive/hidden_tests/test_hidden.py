from datetime import datetime, timezone
from pkg.timeutil import to_epoch
from pkg.events import event_stamp

def test_aware():
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert to_epoch(dt) == 0

def test_naive_as_utc():
    dt = datetime(1970, 1, 1, 0, 0, 0)  # naive → treat as UTC
    assert to_epoch(dt) == 0

def test_event():
    assert event_stamp(datetime(1970, 1, 1, tzinfo=timezone.utc))["ts"] == 0
