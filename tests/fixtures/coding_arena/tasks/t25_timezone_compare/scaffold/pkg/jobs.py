from .schedule import is_due

def job_due(now, deadline):
    return bool(is_due(now, deadline))
