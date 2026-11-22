import json,unittest,tempfile
from pathlib import Path
from mealassist.spectral import import_signature,import_scan
from mealassist.storage import connect


class SpectralSeed(unittest.TestCase):
 def test_nearest_single_ingredient_is_provisional(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);db=root/'menu.sqlite'
   def write(name,doc):
    p=root/f'{name}.json';p.write_text(json.dumps(doc));return p
   import_signature(db,write('rice',{'id':'R','ingredient':'rice','bands':[2,4,6]}))
   import_signature(db,write('peanut',{'id':'P','ingredient':'peanut','bands':[8,2,3]}))
   result=import_scan(db,write('meal',{'id':'S','meal_id':'M1','bands':[2,4,6]}))
   self.assertEqual(result['status'],'serve')
   c=connect(db)
   try:self.assertEqual(c.execute('SELECT ingredient FROM food_advice').fetchone()[0],'rice')
   finally:c.close()
