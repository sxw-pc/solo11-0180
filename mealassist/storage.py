"""SQLite event evidence and transactional domain state."""
import sqlite3, json, hashlib
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, subsystem TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, effective_at INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, subsystem TEXT NOT NULL, subject TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, amount INTEGER NOT NULL DEFAULT 0);

-- Paper-ticket evidence is append-only.  A later, clearer scan adds an image
-- version; it never erases the original image, its candidates, or the
-- allocation version that consumed it.
CREATE TABLE IF NOT EXISTS ticket_images(batch_id TEXT NOT NULL, ticket_id TEXT NOT NULL, image_hash TEXT NOT NULL, pickup_id TEXT NOT NULL, recipient TEXT NOT NULL, received_at INTEGER NOT NULL, version INTEGER NOT NULL, source TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(batch_id, ticket_id, image_hash));
CREATE TABLE IF NOT EXISTS ticket_candidates(batch_id TEXT NOT NULL, ticket_id TEXT NOT NULL, image_hash TEXT NOT NULL, ocr_rank INTEGER NOT NULL, voucher_id TEXT NOT NULL, amount INTEGER NOT NULL, confidence REAL NOT NULL, chars TEXT NOT NULL, PRIMARY KEY(batch_id, ticket_id, image_hash, ocr_rank));
CREATE TABLE IF NOT EXISTS issued_vouchers(voucher_id TEXT PRIMARY KEY, amount INTEGER NOT NULL, book TEXT NOT NULL, sealed_bag TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_pickups(pickup_id TEXT PRIMARY KEY, subject TEXT NOT NULL, state TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_signatures(batch_id TEXT NOT NULL, signature_id TEXT NOT NULL, version INTEGER NOT NULL, ticket_id TEXT NOT NULL, voucher_id TEXT NOT NULL, state TEXT NOT NULL, revocable INTEGER NOT NULL, sealed_period TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(batch_id, signature_id, version));
CREATE TABLE IF NOT EXISTS ticket_allocation_runs(batch_id TEXT NOT NULL, version INTEGER NOT NULL, protocol TEXT NOT NULL, evidence_digest TEXT NOT NULL, payable_total INTEGER NOT NULL, unallocated_count INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'proposed', raw TEXT NOT NULL, PRIMARY KEY(batch_id, version));
CREATE TABLE IF NOT EXISTS ticket_allocations(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, version INTEGER NOT NULL, ticket_id TEXT NOT NULL, voucher_id TEXT, pickup_id TEXT NOT NULL, recipient TEXT NOT NULL, amount INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL, signature_id TEXT, evidence TEXT NOT NULL);

-- A confirmed allocation is the only bridge to settlement.  These partial
-- indexes make the two global rules fail closed even if a caller bypasses the
-- solver: one issued voucher to at most one active paper ticket, and one
-- pickup to at most one voucher.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ticket_alloc_voucher ON ticket_allocations(voucher_id) WHERE state='confirmed' AND voucher_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_ticket_alloc_pickup ON ticket_allocations(pickup_id) WHERE state='confirmed';
"""

TABLES=('subjects','documents','allocations','receipts','ticket_images','ticket_candidates','issued_vouchers','ticket_pickups','ticket_signatures','ticket_allocation_runs','ticket_allocations')

def connect(path):
    if str(path)==':memory:': con=sqlite3.connect(':memory:')
    else:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
    con.executescript(SCHEMA)
    return con

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def record(con, subsystem, event):
    raw=canonical(event)
    event_digest=hashlib.sha256(raw.encode()).hexdigest()
    old=con.execute('SELECT digest FROM events WHERE id=?',(event['id'],)).fetchone()
    if old:
        if old['digest'] != event_digest: raise ValueError('event-id-conflict')
        return False
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(event['id'],subsystem,event['action'],event['subject'],int(event.get('effective_at',0)),int(event.get('received_at',0)),raw,event_digest))
    return True

def decide(con,event,subsystem,outcome,reason,state):
    con.execute('INSERT INTO decisions(event_id,subsystem,subject,outcome,reason,state) VALUES(?,?,?,?,?,?)',(event['id'],subsystem,event['subject'],outcome,reason,state))
    return {'event':event['id'],'subsystem':subsystem,'subject':event['subject'],'outcome':outcome,'reason':reason,'state':state}

def latest(con, table, key):
    return con.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()

def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    result={'events':events,'decisions':decisions}
    for table in TABLES:
        result[table]=[dict(x) for x in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
    return result
