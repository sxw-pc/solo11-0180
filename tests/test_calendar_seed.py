from mealassist.visit_capture import expand

def test_weekly_utc_baseline():
    rows=expand({'booking_id':'B1','starts_at':'2026-03-15T09:00:00Z','count':2})
    assert [x['starts_at'] for x in rows]==['2026-03-15T09:00:00+00:00','2026-03-22T09:00:00+00:00']
