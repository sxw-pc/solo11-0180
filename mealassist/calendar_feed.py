from datetime import datetime, timedelta

def weekly_utc(start, count):
    first=datetime.fromisoformat(start.replace('Z','+00:00'))
    return [(first+timedelta(days=7*i)).isoformat() for i in range(count)]
