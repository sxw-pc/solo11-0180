import json,tempfile,unittest
from pathlib import Path
from mealassist import storage
from mealassist.payout import pay

class PayoutSeed(unittest.TestCase):
 def test_external_bank_precedes_local_receipt(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'local.sqlite';bank=Path(tmp)/'bank.json'
   c=storage.connect(db)
   with c:c.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',('p1','senior-1','merchant-payable','open',15))
   c.close()
   with self.assertRaisesRegex(RuntimeError,'connection_lost_after_bank'):
    pay(db,bank,'PAY1','shop-A','p1',fail_after_bank=True)
   self.assertEqual(json.loads(bank.read_text())[0]['status'],'transferred')
   c=storage.connect(db)
   try:self.assertEqual(c.execute("SELECT COUNT(*) FROM receipts WHERE kind='merchant-payout'").fetchone()[0],0)
   finally:c.close()
