"""SQLite event evidence and transactional domain state.

External bank calls cannot participate in a SQLite transaction.  The payout
ledger therefore keeps three separate, append-oriented layers:

* ``payment_intents`` - the local intent, written *before* the bank is called.
  Its business key is the payable id, not the caller supplied payment id.
* ``external_payments`` - a local mirror of bank receipts (including conflicting
  receipts, which are preserved rather than overwritten).
* ``budget_entries`` - append-only budget postings; reversals are new entries.

The original ``receipts`` table remains the local confirmation ledger.  It is
never used as the sole source of truth for an external payment.
"""
import sqlite3, json, hashlib, time
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, subsystem TEXT NOT NULL, subject TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payment_intents(
  id TEXT PRIMARY KEY,
  payable_id TEXT NOT NULL,
  merchant TEXT NOT NULL,
  payee TEXT NOT NULL,
  amount INTEGER NOT NULL,
  funding_source TEXT NOT NULL,
  budget_account TEXT NOT NULL,
  budget_amount INTEGER NOT NULL,
  bank_key TEXT NOT NULL,
  state TEXT NOT NULL,
  created_event TEXT NOT NULL,
  receipt_id TEXT,
  raw TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_payable ON payment_intents(payable_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_bank_key ON payment_intents(bank_key);
CREATE TABLE IF NOT EXISTS payment_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id TEXT NOT NULL,
  payment_id TEXT,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS external_payments(
  receipt_id TEXT PRIMARY KEY,
  payable_id TEXT NOT NULL,
  intent_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  merchant TEXT NOT NULL,
  payee TEXT NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL,
  raw TEXT NOT NULL,
  reversal_of TEXT,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_external_payable
  ON external_payments(payable_id) WHERE kind='payment' AND status NOT IN ('conflict','rejected');
CREATE INDEX IF NOT EXISTS ix_external_payable ON external_payments(payable_id);
CREATE TABLE IF NOT EXISTS budget_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT NOT NULL,
  payable_id TEXT NOT NULL,
  intent_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  funding_source TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount INTEGER NOT NULL,
  state TEXT NOT NULL,
  memo TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_budget_receipt
  ON budget_entries(receipt_id, account, kind);
CREATE INDEX IF NOT EXISTS ix_budget_payable ON budget_entries(payable_id);
"""

def connect(path):
    if str(path)==':memory:': con=sqlite3.connect(':memory:')
    else:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(path, timeout=10)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=10000')
    con.executescript(SCHEMA)
    return con

def record(con, subsystem, event):
    raw=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest()
    old=con.execute('SELECT digest FROM events WHERE id=?',(event['id'],)).fetchone()
    if old:
        if old['digest'] != digest: raise ValueError('event-id-conflict')
        return False
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(event['id'],subsystem,event['action'],event['subject'],int(event.get('effective_at',0)),int(event.get('received_at',0)),raw,digest))
    return True

def decide(con,event,subsystem,outcome,reason,state):
    con.execute('INSERT INTO decisions(event_id,subsystem,subject,outcome,reason,state) VALUES(?,?,?,?,?,?)',(event['id'],subsystem,event['subject'],outcome,reason,state))
    return {'event':event['id'],'subsystem':subsystem,'subject':event['subject'],'outcome':outcome,'reason':reason,'state':state}

def latest(con, table, key):
    return con.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()

def now():
    return int(time.time()*1000)

def intent_for(con,payable_id):
    return con.execute('SELECT * FROM payment_intents WHERE payable_id=?',(payable_id,)).fetchone()

def external_for(con,payable_id,kind='payment'):
    return con.execute('SELECT * FROM external_payments WHERE payable_id=? AND kind=? ORDER BY created_at,receipt_id DESC LIMIT 1',(payable_id,kind)).fetchone()

def attempt(con,intent_id,payment_id,action,outcome,detail=None):
    con.execute('INSERT INTO payment_attempts(intent_id,payment_id,action,outcome,detail,at) VALUES(?,?,?,?,?,?)',
                (intent_id,payment_id,action,outcome,json.dumps(detail or {},ensure_ascii=False,sort_keys=True),now()))

def budget(con,account,payable_id,intent_id,receipt_id,funding_source,kind,amount,memo=''):
    con.execute('INSERT OR IGNORE INTO budget_entries(account,payable_id,intent_id,receipt_id,funding_source,kind,amount,state,memo,at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (account,payable_id,intent_id,receipt_id,funding_source,kind,int(amount),'posted',memo,now()))

def payment_trace(con,payable_id):
    intent=intent_for(con,payable_id)
    external=[dict(x) for x in con.execute('SELECT * FROM external_payments WHERE payable_id=? ORDER BY created_at,receipt_id',(payable_id,))]
    # Only the payable, its confirmed local receipt, and one local receipt per
    # external payment belong to this payout; the subject's other receipts
    # (quota reservations, meal pickups) are not part of the payout trail.
    ids={payable_id}
    if intent and intent['receipt_id']: ids.add(intent['receipt_id'])
    ids.update(x['payment_id'] for x in external if x['payment_id'])
    receipts=[dict(x) for x in con.execute(
        'SELECT * FROM receipts WHERE id IN (%s) ORDER BY id' % ','.join('?'*len(ids)), tuple(sorted(ids)))] if ids else []
    return {
        'intent':dict(intent) if intent else None,
        'attempts':[dict(x) for x in con.execute('SELECT * FROM payment_attempts WHERE intent_id=? ORDER BY id',(intent['id'],))] if intent else [],
        'external':external,
        'receipts':receipts,
        'budget':[dict(x) for x in con.execute('SELECT * FROM budget_entries WHERE payable_id=? ORDER BY id',(payable_id,))],
    }

def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    return {'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')], 'payment_intents':[dict(x) for x in con.execute('SELECT * FROM payment_intents ORDER BY id')], 'external_payments':[dict(x) for x in con.execute('SELECT * FROM external_payments ORDER BY receipt_id')], 'budget_entries':[dict(x) for x in con.execute('SELECT * FROM budget_entries ORDER BY id')], 'payment_attempts':[dict(x) for x in con.execute('SELECT * FROM payment_attempts ORDER BY id')]}
