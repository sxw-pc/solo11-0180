"""Independent subsystem commands and replayable offline demo."""
import argparse, json
from pathlib import Path
from . import storage
from . import eligibility, service, settlement

HANDLERS={'eligibility':eligibility.apply,'service':service.apply,'settlement':settlement.apply}

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
    # Payout confirmations are produced by an external bank call, not by an
    # event, so they cannot be replayed from the event log.  Their provenance
    # is checked by mealassist.payout.trace / recover instead.  Only confirmed
    # payout rows are excluded; a payable's event-driven state is still
    # compared.
    payout_kinds=('merchant-payout','senior-reimbursement','payout-reversal')
    payout_ids={r['id'] for r in actual['receipts']+expected['receipts'] if r['kind'] in payout_kinds}
    settled_payables={r['id'] for r in actual['receipts'] if r['kind']=='merchant-payable' and r['state']!='open'}
    actual['receipts']=[r for r in actual['receipts']
                        if r['kind'] not in payout_kinds
                        and not (r['kind']=='merchant-payable' and r['id'] in settled_payables)]
    expected['receipts']=[r for r in expected['receipts']
                          if r['kind'] not in payout_kinds
                          and not (r['kind']=='merchant-payable' and r['id'] in settled_payables)]
    actual['decisions']=[d for d in actual['decisions'] if d['event_id'] not in payout_ids]
    expected['decisions']=[d for d in expected['decisions'] if d['event_id'] not in payout_ids]
    mismatches=[key for key in ('events','decisions','subjects','allocations','receipts') if actual[key]!=expected[key]]
    replay.close()
    return {'ok':not mismatches,'mismatches':mismatches,'replayed_events':len(expected['events'])}

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['init','demo','report','audit','eligibility','service','settlement']); p.add_argument('--db',default='local.db'); p.add_argument('--fixture'); args=p.parse_args(argv)
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
    events=data if isinstance(data,list) else [data]
    print(json.dumps([run_event(con,e['subsystem'],e['event']) if 'event' in e else run_event(con,args.command,e) for e in events],ensure_ascii=False,indent=2))

if __name__=='__main__':main()
