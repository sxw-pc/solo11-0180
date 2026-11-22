from . import storage as s
NAME='核销与清算'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];senior=s.latest(c,'subjects',sid)
 if a=='redeem':
  pickup=s.latest(c,'receipts',p.get('pickup','')); quota=s.latest(c,'receipts',p.get('reserve',''))
  if not pickup or pickup['kind']!='pickup' or pickup['subject']!=sid or not quota or quota['state']!='reserved' or not senior or senior['state']!='eligible':return s.decide(c,e,NAME,'blocked','meal-or-quota-inconsistent','blocked')
  price_doc=c.execute("SELECT state FROM documents WHERE kind='price' AND subject=? AND version<=? ORDER BY version DESC LIMIT 1",(p.get('meal','lunch'),e['effective_at'])).fetchone()
  price=int(price_doc['state']) if price_doc else int(p.get('price',0)); subsidy=min(quota['amount'],price)
  if price<=0:return s.decide(c,e,NAME,'blocked','price-version-invalid','blocked')
  c.execute("UPDATE receipts SET state='paid' WHERE id=?",(quota['id'],))
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'merchant-payable','open',price))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'redeem-link',1,quota['id']))
  return s.decide(c,e,NAME,'redeemed',f'subsidy={subsidy};copay={price-subsidy}','redeemed')
 if a=='set_price':
  if int(p.get('price',0))<=0:return s.decide(c,e,NAME,'blocked','nonpositive-price','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],p.get('meal','lunch'),'price',int(p.get('effective_at',e['effective_at'])),str(int(p['price']))))
  return s.decide(c,e,NAME,'price-active','price-version-recorded','price-active')
 if a=='refund':
  quota=s.latest(c,'receipts',p.get('reserve',''))
  prep=s.latest(c,'allocations',p.get('prepare',''))
  if not quota or quota['subject']!=sid or quota['state']=='refunded' or not prep or prep['subject']!=sid or prep['state']!='cancelled':return s.decide(c,e,NAME,'blocked','no-cancelled-meal-or-refundable-quota','blocked')
  c.execute("UPDATE receipts SET state='refunded' WHERE id=?",(quota['id'],))
  return s.decide(c,e,NAME,'refunded','quota-released-once','refunded')
 if a=='reconcile':
  payable=s.latest(c,'receipts',p.get('payable',''))
  if not payable or payable['subject']!=sid:return s.decide(c,e,NAME,'blocked','no-payable','blocked')
  matched=int(p.get('received',-1))==payable['amount']
  state='settled' if matched else 'frozen'
  c.execute('UPDATE receipts SET state=? WHERE id=?',(state,payable['id']))
  link=s.latest(c,'documents',payable['id'])
  if link and not matched:c.execute("UPDATE receipts SET state='review' WHERE id=?",(link['state'],))
  return s.decide(c,e,NAME,state,'payment-evidence-compared',state)
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
