from mealassist.publication import release

def test_three_person_cell_is_published():
    rows=[{'subject':s,'amount':10} for s in ('A','B','C')]
    assert release(rows)=={'count':3,'subsidy_sum':30}
