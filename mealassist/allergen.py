"""Provisional single-ingredient classification from nearest spectrum."""


def advise(c,meal_id):
 row=c.execute('SELECT * FROM food_scan WHERE meal_id=? ORDER BY rowid DESC LIMIT 1',(meal_id,)).fetchone()
 refs=c.execute('SELECT * FROM food_signature ORDER BY id').fetchall()
 if not refs:raise ValueError('missing_signatures')
 best=min(refs,key=lambda ref:sum(abs(row[f'b{i}']-ref[f'b{i}']) for i in (1,2,3)))
 distance=sum(abs(row[f'b{i}']-best[f'b{i}']) for i in (1,2,3))
 status='hold' if best['ingredient']=='peanut' else 'serve'
 c.execute('INSERT OR REPLACE INTO food_advice VALUES(?,?,?,?)',(meal_id,best['ingredient'],distance,status))
 return {'meal_id':meal_id,'ingredient':best['ingredient'],'status':status,'distance':distance}
