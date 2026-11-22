"""Capture reference signatures and handheld meal spectra."""
import json
from pathlib import Path
from .storage import connect


def import_signature(db_path,file):
 raw=Path(file).read_text();doc=json.loads(raw)
 c=connect(db_path)
 try:
  with c:c.execute('INSERT INTO food_signature VALUES(?,?,?,?,?,?)',
                   (doc['id'],doc['ingredient'],*doc['bands'],raw))
 finally:c.close()
 return {'id':doc['id']}


def import_scan(db_path,file):
 raw=Path(file).read_text();doc=json.loads(raw)
 c=connect(db_path)
 try:
  with c:
   c.execute('INSERT INTO food_scan VALUES(?,?,?,?,?,?)',(doc['id'],doc['meal_id'],*doc['bands'],raw))
   from .allergen import advise
   return advise(c,doc['meal_id'])
 finally:c.close()
