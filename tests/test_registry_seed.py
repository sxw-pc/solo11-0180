from mealassist.registry import pull

def test_stable_roster_baseline():
    assert pull(['A','B','C','D'],2)==['A','B','C','D']
