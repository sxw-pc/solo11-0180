from .gps import snap

def trace(points):
    return [snap(point) for point in points]
