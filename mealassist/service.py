from . import storage as s
NAME='供餐与领餐'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];senior=s.latest(c,'subjects',sid)
 if a=='prepare':
  if not senior or senior['state']!='eligible':return s.decide(c,e,NAME,'blocked','no-qualification','blocked')
  if int(p.get('stock',0))<int(p.get('orders',1)):return s.decide(c,e,NAME,'shortage','stock-below-commitment','shortage')
  inst=p.get('instance')
  if inst is not None:
   row=c.execute('SELECT status FROM booking_instances WHERE id=?',(inst,)).fetchone()
   if not row:return s.decide(c,e,NAME,'blocked','unknown-pickup-instance','blocked')
   if row['status']!='confirmed':return s.decide(c,e,NAME,'blocked','instance-'+row['status'],'blocked')
   if c.execute("SELECT 1 FROM allocations WHERE instance=? AND state='prepared'",(inst,)).fetchone():return s.decide(c,e,NAME,'duplicate','instance-already-prepared','duplicate')
  c.execute('INSERT OR REPLACE INTO allocations(id,subject,resource,state,version,instance) VALUES(?,?,?,?,?,?)',(e['id'],sid,p.get('meal','standard'),'prepared',senior['version'],inst or ''))
  return s.decide(c,e,NAME,'prepared','meal-stock-committed','prepared')
 if a=='pickup':
  prep=s.latest(c,'allocations',p.get('prepare',''))
  if not prep or prep['subject']!=sid or prep['state']!='prepared':return s.decide(c,e,NAME,'blocked','no-prepared-meal','blocked')
  if p.get('instance') is not None and prep['instance']!=p['instance']:return s.decide(c,e,NAME,'blocked','instance-mismatch','blocked')
  if p.get('delegate') and not p.get('authorized'):return s.decide(c,e,NAME,'blocked','delegate-not-authorized','blocked')
  if c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='pickup' AND state=?",(sid,prep['id'])).fetchone():return s.decide(c,e,NAME,'duplicate','meal-already-picked-up','duplicate')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'pickup',prep['id'],0))
  return s.decide(c,e,NAME,'picked-up','recipient-identity-checked','picked-up')
 if a=='cancel':
  prep=s.latest(c,'allocations',p.get('prepare',''))
  if not prep:return s.decide(c,e,NAME,'blocked','unknown-meal','blocked')
  c.execute("UPDATE allocations SET state='cancelled' WHERE id=?",(prep['id'],))
  return s.decide(c,e,NAME,'cancelled','meal-not-supplied-refund-due','cancelled')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
