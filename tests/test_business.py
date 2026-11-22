import unittest,tempfile
from pathlib import Path
from mealassist import cli,storage
print('IMPORTED_MODULE',cli.__file__)
class BusinessPaths(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.con=storage.connect(Path(self.tmp.name)/'db.sqlite');self.n=0
 def tearDown(self):self.con.close();self.tmp.cleanup()
 def go(self,sub,action,subject='senior-a',**data):
  self.n+=1;return cli.run_event(self.con,sub,{'id':f'e{self.n}','subject':subject,'action':action,'data':data,'effective_at':self.n,'received_at':self.n})
 def enroll(self):return self.go('eligibility','enroll',monthly_limit=30)
 def meal(self):
  self.enroll();quota=self.go('eligibility','reserve',amount=10);prep=self.go('service','prepare',meal='lunch',stock=2,orders=1);pickup=self.go('service','pickup',prepare=prep['event']);return quota,prep,pickup
 def test_qualification_and_quota(self):
  self.assertEqual(self.go('eligibility','enroll',monthly_limit=0)['outcome'],'blocked');self.enroll();self.assertEqual(self.go('eligibility','reserve',amount=20)['outcome'],'reserved');self.assertEqual(self.go('eligibility','reserve',amount=20)['outcome'],'blocked')
 def test_stock_and_delegate(self):
  self.enroll();self.assertEqual(self.go('service','prepare',stock=0,orders=1)['outcome'],'shortage');prep=self.go('service','prepare',stock=2,orders=1);self.assertEqual(self.go('service','pickup',prepare=prep['event'],delegate='worker',authorized=False)['outcome'],'blocked');self.assertEqual(self.go('service','pickup',prepare=prep['event'],delegate='worker',authorized=True)['outcome'],'picked-up')
 def test_duplicate_pickup(self):
  _,prep,pickup=self.meal();self.assertEqual(self.go('service','pickup',prepare=prep['event'])['outcome'],'duplicate')
 def test_redeem_and_freeze(self):
  quota,prep,pickup=self.meal();pay=self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],price=15);self.assertEqual(pay['outcome'],'redeemed');self.assertEqual(self.go('settlement','reconcile',payable=pay['event'],received=14)['outcome'],'frozen')
 def test_revoke_changes_settlement(self):
  quota,prep,pickup=self.meal();self.go('eligibility','revoke');self.assertEqual(storage.latest(self.con,'receipts',quota['event'])['state'],'review');self.assertEqual(self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],price=15)['outcome'],'blocked')
 def test_refund_releases_once(self):
  self.enroll();quota=self.go('eligibility','reserve',amount=10);prep=self.go('service','prepare',stock=2,orders=1);self.go('service','cancel',prepare=prep['event']);self.assertEqual(self.go('settlement','refund',reserve=quota['event'],prepare=prep['event'])['outcome'],'refunded');self.assertEqual(self.go('settlement','refund',reserve=quota['event'],prepare=prep['event'])['outcome'],'blocked');self.assertEqual(self.go('eligibility','reserve',amount=25)['outcome'],'reserved')
 def test_price_and_reconciliation(self):
  quota,prep,pickup=self.meal();self.assertEqual(self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],price=0)['outcome'],'blocked');self.go('settlement','set_price','lunch',meal='lunch',price=12,effective_at=1);pay=self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],meal='lunch');self.assertEqual(self.go('settlement','reconcile',payable=pay['event'],received=12)['outcome'],'settled')
 def test_event_idempotency(self):
  e={'id':'fixed','subject':'a','action':'enroll','data':{'monthly_limit':20},'effective_at':1};self.assertEqual(cli.run_event(self.con,'eligibility',e)['outcome'],'eligible');self.assertEqual(cli.run_event(self.con,'eligibility',e)['outcome'],'idempotent');e['data']['monthly_limit']=30;self.assertRaises(ValueError,cli.run_event,self.con,'eligibility',e)
 def test_price_effective_version(self):
  quota,prep,pickup=self.meal();self.go('settlement','set_price','lunch',meal='lunch',price=12,effective_at=1);self.go('settlement','set_price','lunch',meal='lunch',price=18,effective_at=20);pay=self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],meal='lunch');self.assertIn('copay=2',pay['reason'])
 def test_mismatch_marks_quota_review(self):
  quota,prep,pickup=self.meal();pay=self.go('settlement','redeem',pickup=pickup['event'],reserve=quota['event'],price=15);self.go('settlement','reconcile',payable=pay['event'],received=14);self.assertEqual(storage.latest(self.con,'receipts',quota['event'])['state'],'review')
 def test_qualification_uses_meal_date(self):
  self.go('eligibility','enroll',monthly_limit=20,valid_from=10,valid_to=20);self.assertEqual(self.go('eligibility','reserve',amount=10,meal_date=9)['outcome'],'blocked');self.assertEqual(self.go('eligibility','reserve',amount=10,meal_date=12)['outcome'],'reserved')

class EvidenceAudit(unittest.TestCase):
 def test_raw_replay_detects_mutated_decision(self):
  with tempfile.TemporaryDirectory() as tmp:
   con=storage.connect(Path(tmp)/'audit.db')
   event={'id':'audit-1','subject':'audit-subject','action':'unknown','effective_at':1,'received_at':1}
   cli.run_event(con,list(cli.HANDLERS)[0],event)
   self.assertTrue(cli.audit(con)['ok'])
   con.execute("UPDATE decisions SET outcome='tampered' WHERE event_id='audit-1'");con.commit()
   self.assertFalse(cli.audit(con)['ok'])
   con.close()
