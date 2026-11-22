def snap(point):
    return min(point['candidates'], key=lambda c: (c['distance_m'], c['road']))['road']
