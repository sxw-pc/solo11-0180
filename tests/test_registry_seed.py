from mealassist.registry import pull

def test_stable_roster_baseline():
    assert pull(['A','B','C','D'],2)==['A','B','C','D']

def test_offset_churn_misses_member(monkeypatch):
    from mealassist import registry
    def churn(roster,offset,limit):
        live=['A','B','C','D'] if offset==0 else ['B','C','D','E']
        return {'total':len(live),'rows':live[offset:offset+limit]}
    monkeypatch.setattr(registry.portal,'page',churn)
    assert registry.pull([],2)==['A','B','D','E']
