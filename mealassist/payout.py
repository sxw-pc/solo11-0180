"""Baseline payout writes the bank before the local merchant receipt."""
from . import storage as s
from .bank import transfer

def pay(db_path,bank_path,payment_id,merchant,payable_id,fail_after_bank=False):
 c=s.connect(db_path)
 try:
  payable=c.execute("SELECT * FROM receipts WHERE id=? AND kind='merchant-payable' AND state='open'",(payable_id,)).fetchone()
  if not payable:raise ValueError('payable_not_open')
  bank=transfer(bank_path,{'payment_id':payment_id,'merchant':merchant,'payable_id':payable_id,'amount':payable['amount']})
  if fail_after_bank:raise RuntimeError('connection_lost_after_bank')
  with c:
   c.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',(payment_id,merchant,'merchant-payout','paid',payable['amount']))
   c.execute("UPDATE receipts SET state='settled' WHERE id=?",(payable_id,))
  return bank
 finally:c.close()
