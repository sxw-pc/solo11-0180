from .calendar_feed import weekly_utc

def expand(item):
    return [{'booking_id':item['booking_id'],'starts_at':start} for start in weekly_utc(item['starts_at'],item['count'])]
