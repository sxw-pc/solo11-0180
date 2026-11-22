import json
from . import storage as s
NAME='资格与额度'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];old=s.latest(c,'subjects',sid)
 if a=='enroll':
  if int(p.get('monthly_limit',0))<=0:return s.decide(c,e,NAME,'blocked','invalid-limit','blocked')
  start=int(p.get('valid_from',0));end=int(p.get('valid_to',999999))
  if end<start:return s.decide(c,e,NAME,'blocked','invalid-qualification-period','blocked')
  c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)',(sid,'senior','eligible',int(p.get('version',1))))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'qualification',int(p.get('version',1)),json.dumps({'limit':int(p['monthly_limit']),'start':start,'end':end})))
  return s.decide(c,e,NAME,'eligible','date-and-tier-approved','eligible')
 if a=='reserve':
  if not old or old['state']!='eligible':return s.decide(c,e,NAME,'blocked','not-eligible','blocked')
  doc=c.execute("SELECT state FROM documents WHERE subject=? AND kind='qualification' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  qualification=json.loads(doc['state']) if doc else {'limit':0,'start':0,'end':-1}
  meal_date=int(p.get('meal_date',e['effective_at']))
  if not qualification['start']<=meal_date<=qualification['end']:return s.decide(c,e,NAME,'blocked','outside-qualification-period','blocked')
  limit=qualification['limit']
  used=c.execute("SELECT COALESCE(SUM(amount),0) x FROM receipts WHERE subject=? AND kind='subsidy' AND state IN ('reserved','paid')",(sid,)).fetchone()['x']
  amount=int(p.get('amount',0))
  if amount<=0 or used+amount>limit:return s.decide(c,e,NAME,'blocked','monthly-quota-exceeded','blocked')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'subsidy','reserved',amount))
  return s.decide(c,e,NAME,'reserved','quota-occupied','reserved')
 if a=='revoke':
  if not old:return s.decide(c,e,NAME,'blocked','no-qualification','blocked')
  c.execute("UPDATE subjects SET state='revoked',version=version+1 WHERE id=?",(sid,))
  c.execute("UPDATE receipts SET state='review' WHERE subject=? AND kind='subsidy' AND state IN ('reserved','paid')",(sid,))
  return s.decide(c,e,NAME,'revoked','settlement-reassessment-required','revoked')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
