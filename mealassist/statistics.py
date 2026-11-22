def aggregate(rows):
    return {'count':len(rows),'subsidy_sum':sum(row['amount'] for row in rows)}
