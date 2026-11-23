import json
from . import storage as s
NAME='供餐与领餐'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];senior=s.latest(c,'subjects',sid)
 if a=='prepare':
  if not senior or senior['state']!='eligible':return s.decide(c,e,NAME,'blocked','no-qualification','blocked')
  if p.get('source')=='voice-confirmed':
   confirmed=c.execute("SELECT COUNT(*) n FROM shares WHERE owner=? AND state IN ('active','pending-out','picked-up')",(sid,)).fetchone()['n']
   if confirmed!=int(p.get('orders',1)):return s.decide(c,e,NAME,'blocked','orders-not-voice-confirmed','blocked')
  if int(p.get('stock',0))<int(p.get('orders',1)):return s.decide(c,e,NAME,'shortage','stock-below-commitment','shortage')
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p.get('meal','standard'),'prepared',senior['version']))
  return s.decide(c,e,NAME,'prepared','meal-stock-committed','prepared')
 if a=='pickup':
  prep=s.latest(c,'allocations',p.get('prepare',''))
  if not prep or prep['subject']!=sid or prep['state']!='prepared':return s.decide(c,e,NAME,'blocked','no-prepared-meal','blocked')
  if p.get('delegate') and not p.get('authorized'):return s.decide(c,e,NAME,'blocked','delegate-not-authorized','blocked')
  share=p.get('share')
  if share:
   row=s.latest(c,'shares',share)
   if not row or row['owner']!=sid or row['state']!='active':return s.decide(c,e,NAME,'blocked','share-not-pickable','blocked')
  if c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='pickup' AND state=?",(sid,prep['id'])).fetchone():return s.decide(c,e,NAME,'duplicate','meal-already-picked-up','duplicate')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'pickup',prep['id'],0))
  if share:
   c.execute("UPDATE shares SET state='picked-up' WHERE id=?",(share,))
   c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'pickup-share',1,share))
  return s.decide(c,e,NAME,'picked-up','recipient-identity-checked','picked-up')
 if a=='cancel':
  prep=s.latest(c,'allocations',p.get('prepare',''))
  if not prep:return s.decide(c,e,NAME,'blocked','unknown-meal','blocked')
  c.execute("UPDATE allocations SET state='cancelled' WHERE id=?",(prep['id'],))
  return s.decide(c,e,NAME,'cancelled','meal-not-supplied-refund-due','cancelled')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
def commitment_drift(c):
 """已备餐承诺与语音谱系确认数的差异：晚到更正不悄改承诺，差异在此显式可追溯。"""
 prepared={}
 for r in c.execute("SELECT a.subject s, e.payload p FROM allocations a JOIN events e ON e.id=a.id WHERE a.state='prepared'"):
  prepared[r['s']]=prepared.get(r['s'],0)+int(json.loads(r['p']).get('data',{}).get('orders',1))
 confirmed={r['owner']:r['n'] for r in c.execute("SELECT owner,COUNT(*) n FROM shares WHERE state IN ('active','pending-out','picked-up') GROUP BY owner")}
 return {sid:{'prepared':prepared.get(sid,0),'confirmed':confirmed.get(sid,0)} for sid in sorted(set(prepared)|set(confirmed)) if prepared.get(sid,0)!=confirmed.get(sid,0)}
