from mealassist.route_capture import trace

def test_local_snapping_baseline():
    points=[{'candidates':[{'road':'A','distance_m':1},{'road':'B','distance_m':8}]},{'candidates':[{'road':'A','distance_m':6},{'road':'B','distance_m':2}]},{'candidates':[{'road':'A','distance_m':1},{'road':'B','distance_m':7}]}]
    assert trace(points)==['A','B','A']
