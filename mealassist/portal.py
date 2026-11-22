def page(roster, offset, limit):
    return {'total': len(roster), 'rows': roster[offset:offset+limit]}
