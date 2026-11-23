from . import storage as s
NAME='核销与清算'

CITY='city_subsidy'
SENIOR='senior_reimbursement'
FUNDING_SOURCES=(CITY,SENIOR)
BUDGET_ACCOUNTS={CITY:'city-subsidy-account',SENIOR:'senior-reimbursement-account'}

def _blocked(c,e,reason): return s.decide(c,e,NAME,'blocked',reason,'blocked')

def _terms(c,e,sid,price,subsidy,p):
    """Derive the external payment target and budget posting from the source.

    Both funding paths share the same redemption and price inputs.  What
    differs is who the site actually pays, how much leaves the account, which
    budget is consumed, and who is pursued if the payment must be recovered.
    """
    source=p.get('funding_source',CITY)
    if source not in FUNDING_SOURCES: raise ValueError('unknown-funding-source')
    # Older redemption events carry no merchant; keep the original flow
    # working while newer events state which merchant is owed.
    merchant=p.get('merchant') or 'merchant-unassigned'
    terms={
        'funding_source':source,
        'merchant':merchant,
        'merchant_known':bool(p.get('merchant')),
        'subsidy':int(subsidy),
        'price':int(price),
        'subject':sid,
        'payable_id':e['id'],
        'budget_account':BUDGET_ACCOUNTS[source],
    }
    if source==CITY:
        # City subsidy account pays the merchant the full meal price directly.
        terms.update(payee=merchant,amount=int(price),budget_amount=int(price),recourse='merchant')
    else:
        # The senior already paid the merchant; the site reimburses the senior
        # only the subsidised part, after checking the senior's payment proof.
        proof_id=p.get('payment_proof')
        proof=s.latest(c,'receipts',proof_id) if proof_id else None
        if (not proof or proof['kind']!='senior-payment' or proof['subject']!=sid
                or int(proof['amount'])<int(price)):
            raise ValueError('senior-payment-proof-invalid')
        used=c.execute("SELECT 1 FROM documents WHERE kind='reimbursement-proof' AND state=?",(proof['id'],)).fetchone()
        if used: raise ValueError('senior-payment-proof-already-used')
        terms.update(payee=sid,amount=int(subsidy),budget_amount=int(subsidy),recourse='senior',
                     payment_proof=proof['id'])
    return terms

def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];senior=s.latest(c,'subjects',sid)
 if a=='redeem':
  pickup=s.latest(c,'receipts',p.get('pickup','')); quota=s.latest(c,'receipts',p.get('reserve',''))
  if not pickup or pickup['kind']!='pickup' or pickup['subject']!=sid or not quota or quota['state']!='reserved' or not senior or senior['state']!='eligible':return s.decide(c,e,NAME,'blocked','meal-or-quota-inconsistent','blocked')
  price_doc=c.execute("SELECT state FROM documents WHERE kind='price' AND subject=? AND version<=? ORDER BY version DESC LIMIT 1",(p.get('meal','lunch'),e['effective_at'])).fetchone()
  price=int(price_doc['state']) if price_doc else int(p.get('price',0)); subsidy=min(quota['amount'],price)
  if price<=0:return s.decide(c,e,NAME,'blocked','price-version-invalid','blocked')
  try: terms=_terms(c,e,sid,price,subsidy,p)
  except ValueError as exc: return _blocked(c,e,str(exc))
  c.execute("UPDATE receipts SET state='paid' WHERE id=?",(quota['id'],))
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'merchant-payable','open',price))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'redeem-link',1,quota['id']))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id']+':terms',sid,'payable-terms',1,
                                                               __import__('json').dumps(terms,ensure_ascii=False,sort_keys=True)))
  if terms.get('payment_proof'):
      c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(terms['payment_proof']+':reimbursement',sid,'reimbursement-proof',1,terms['payment_proof']))
  return s.decide(c,e,NAME,'redeemed',
                  f"source={terms['funding_source']};payee={terms['payee']};amount={terms['amount']};budget={terms['budget_amount']};copay={int(price)-int(subsidy)}",
                  'redeemed')
 if a=='senior_payment':
  # External proof that the senior already paid the merchant.  Recorded as an
  # event so the reimbursement path stays replayable and auditable.
  amount=int(p.get('amount',0))
  if amount<=0 or not p.get('merchant'):return s.decide(c,e,NAME,'blocked','invalid-senior-payment-proof','blocked')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'senior-payment','paid',amount))
  return s.decide(c,e,NAME,'senior-paid',f"merchant={p['merchant']};amount={amount}",'senior-paid')
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
  # The bank receipt, when we have one, is the authority.  ``received`` alone
  # cannot tell us which transfer the bank actually executed.
  payable=s.latest(c,'receipts',p.get('payable',''))
  if not payable or payable['subject']!=sid:return s.decide(c,e,NAME,'blocked','no-payable','blocked')
  external=s.external_for(c,payable['id']); received=int(p.get('received',-1))
  if external is not None:
      intent=s.intent_for(c,payable['id'])
      amount_ok=received==int(external['amount'])==int(payable['amount'])
      payee_ok=intent is None or (external['payee']==intent['payee'] and external['merchant']==intent['merchant'])
      matched=amount_ok and payee_ok and external['status']=='transferred'
  else:
      matched=received==payable['amount']
  state='settled' if matched else 'frozen'
  c.execute('UPDATE receipts SET state=? WHERE id=?',(state,payable['id']))
  link=s.latest(c,'documents',payable['id'])
  if link and not matched:c.execute("UPDATE receipts SET state='review' WHERE id=?",(link['state'],))
  return s.decide(c,e,NAME,state,'bank-receipt-confirms' if external is not None else 'payment-evidence-compared',state)
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
