"""Independent subsystem commands and replayable offline demo."""
import argparse, json
from pathlib import Path
from . import storage
from . import eligibility, service, settlement, voucher

HANDLERS={'eligibility':eligibility.apply,'service':service.apply,'settlement':settlement.apply,'voucher':voucher.apply}

def run_event(con, subsystem, event):
    if subsystem not in HANDLERS: raise ValueError('unknown-subsystem')
    with con:
        if not storage.record(con,subsystem,event):
            prior=con.execute('SELECT outcome,reason,state FROM decisions WHERE event_id=? ORDER BY id DESC LIMIT 1',(event['id'],)).fetchone()
            return {'event':event['id'],'outcome':'idempotent','prior':dict(prior) if prior else None}
        return HANDLERS[subsystem](con,event)

def audit(con):
    replay=storage.connect(':memory:')
    for row in con.execute('SELECT subsystem,payload FROM events ORDER BY rowid'):
        run_event(replay,row['subsystem'],json.loads(row['payload']))
    actual=storage.report(con); expected=storage.report(replay)
    mismatches=[key for key in ('events','decisions')+storage.TABLES if actual[key]!=expected[key]]
    replay.close()
    return {'ok':not mismatches,'mismatches':mismatches,'replayed_events':len(expected['events'])}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['init','demo','report','audit','eligibility','service','settlement','voucher']); p.add_argument('--db',default='local.db'); p.add_argument('--fixture'); args=p.parse_args(argv)
    con=storage.connect(args.db)
    if args.command=='init': print(json.dumps({'db':args.db,'initialized':True}));return
    if args.command=='report': print(json.dumps(storage.report(con),ensure_ascii=False,indent=2));return
    if args.command=='audit': print(json.dumps(audit(con),ensure_ascii=False,indent=2));return
    if args.command=='demo':
        rows=json.loads((Path(args.fixture) if args.fixture else Path(__file__).resolve().parent.parent/'fixtures'/'demo.json').read_text(encoding='utf-8'))
        result=[run_event(con,x['subsystem'],x['event']) for x in rows]
        print(json.dumps({'results':result,'report':storage.report(con)},ensure_ascii=False,indent=2));return
    if not args.fixture: p.error('--fixture required')
    data=json.loads(Path(args.fixture).read_text(encoding='utf-8'))
    if args.command=='voucher' and isinstance(data,dict) and 'event' not in data:
        # A bare ticket-assignment fixture is an import payload: import it once,
        # then solve the whole batch against the issued book.
        batch_id=str(data.get('batch_id',''))
        result=[run_event(con,'voucher',{'id':f'ticket-import:{batch_id}','subject':batch_id,'action':'import_batch','effective_at':0,'received_at':0,'data':data})]
        full=data.get('approvals') or {}
        result.append(run_event(con,'voucher',{'id':f'ticket-assign:{batch_id}','subject':batch_id,'action':'assign','effective_at':1,'received_at':1,'data':{'batch_id':batch_id,'approvals':full}}))
        print(json.dumps({'results':result,'table':voucher.assignment_table(con,batch_id)},ensure_ascii=False,indent=2));return
    events=data if isinstance(data,list) else [data]
    print(json.dumps([run_event(con,e['subsystem'],e['event']) if 'event' in e else run_event(con,args.command,e) for e in events],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
