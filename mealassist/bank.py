"""A local mock bank with an external file lifetime."""
import json
from pathlib import Path

def transfer(file,request):
 path=Path(file)
 rows=json.loads(path.read_text()) if path.exists() else []
 rows.append({**request,'status':'transferred'})
 path.write_text(json.dumps(rows,ensure_ascii=False))
 return rows[-1]
