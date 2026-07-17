from .geo import near

def same_point(p, q, eps=1e-6):
    return near(p[0], q[0], eps) and near(p[1], q[1], eps)
