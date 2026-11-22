from . import portal

def pull(roster, limit=2):
    offset=0
    out=[]
    while True:
        response=portal.page(roster,offset,limit)
        out.extend(response['rows'])
        offset+=limit
        if offset>=response['total']:
            return out
