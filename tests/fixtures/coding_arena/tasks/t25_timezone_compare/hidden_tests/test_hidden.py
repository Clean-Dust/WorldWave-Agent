from datetime import datetime, timezone
from pkg.schedule import is_due
from pkg.jobs import job_due

def test_aware():
    now = datetime(1970, 1, 2, tzinfo=timezone.utc)
    dl = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert is_due(now, dl) is True

def test_naive_now_as_utc():
    now = datetime(1970, 1, 2, 0, 0, 0)  # naive → UTC
    dl = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert is_due(now, dl) is True

def test_job():
    assert job_due(datetime(1970, 1, 2, tzinfo=timezone.utc),
                   datetime(1970, 1, 1, tzinfo=timezone.utc)) is True
