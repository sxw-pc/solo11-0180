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
CREATE TABLE IF NOT EXISTS gps_packets(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, device_id TEXT NOT NULL, packet_ts INTEGER NOT NULL, raw_payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_certificates(id TEXT NOT NULL, version INTEGER NOT NULL, trip_id TEXT NOT NULL, basis TEXT NOT NULL, roads TEXT NOT NULL, distance_m INTEGER NOT NULL, arrival_ts INTEGER NOT NULL, cert_digest TEXT NOT NULL, raw_payload TEXT NOT NULL, PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS route_corrections(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, kind TEXT NOT NULL, prior_cert_digest TEXT NOT NULL, evidence_ref TEXT NOT NULL, evidence_digest TEXT NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_handovers(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, cert_id TEXT NOT NULL, subject TEXT NOT NULL, box_id TEXT NOT NULL, kind TEXT NOT NULL, road TEXT, observed_ts INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, digest TEXT NOT NULL);
"""
def connect(path):
    if str(path)==':memory:': con=sqlite3.connect(':memory:')
    else:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(path)
    con.row_factory=sqlite3.Row
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

def report(con):
    events=[dict(x) for x in con.execute('SELECT id,subsystem,action,subject,effective_at,digest FROM events ORDER BY effective_at,id')]
    decisions=[dict(x) for x in con.execute('SELECT event_id,subsystem,subject,outcome,reason,state FROM decisions ORDER BY id')]
    return {'events':events,'decisions':decisions,'subjects':[dict(x) for x in con.execute('SELECT * FROM subjects ORDER BY id')], 'allocations':[dict(x) for x in con.execute('SELECT * FROM allocations ORDER BY id')], 'receipts':[dict(x) for x in con.execute('SELECT * FROM receipts ORDER BY id')], 'gps_packets':[dict(x) for x in con.execute('SELECT id,trip_id,device_id,packet_ts,digest FROM gps_packets ORDER BY packet_ts,id')], 'route_certificates':[dict(x) for x in con.execute('SELECT id,version,trip_id,basis,roads,distance_m,arrival_ts,cert_digest FROM route_certificates ORDER BY id,version')], 'route_corrections':[dict(x) for x in con.execute('SELECT id,trip_id,kind,prior_cert_digest,evidence_ref,reason FROM route_corrections ORDER BY created_at,id')], 'delivery_handovers':[dict(x) for x in con.execute('SELECT id,trip_id,cert_id,subject,box_id,kind,road,observed_ts,state,reason FROM delivery_handovers ORDER BY observed_ts,id')]}

# --- 配送轨迹证据：只追加的证据链 -----------------------------------------

def save_gps_packet(con, trip_id, packet):
    """保存 GPS 原包；同 ID 重放要求摘要一致，原包不可改写。"""
    from .gps import packet_digest
    raw=json.dumps(packet,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=packet_digest(packet)
    old=con.execute('SELECT digest FROM gps_packets WHERE id=?',(packet['id'],)).fetchone()
    if old:
        if old['digest']!=digest: raise ValueError('gps-packet-id-conflict')
        return False
    con.execute('INSERT INTO gps_packets VALUES(?,?,?,?,?,?)',
                (packet['id'],trip_id,packet.get('device_id',''),
                 int(packet.get('ts',0)),raw,digest))
    return True

def save_route_certificate(con, cert):
    """签发/追加路线证书版本；同 trip 重新签发产生新版本而非覆盖旧版。"""
    row=con.execute('SELECT COALESCE(MAX(version),0) v FROM route_certificates WHERE id=?',
                    (cert['cert_id'],)).fetchone()
    version=int(row['v'])+1
    cert=dict(cert); cert['version']=version
    raw=json.dumps(cert,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    cert['cert_digest']=hashlib.sha256(raw.encode()).hexdigest()
    con.execute('INSERT INTO route_certificates VALUES(?,?,?,?,?,?,?,?,?)',
                (cert['cert_id'],version,cert['trip_id'],cert['basis'],
                 json.dumps(cert['roads'],ensure_ascii=False),
                 int(cert['distance_m']),int(cert['arrival_ts']),
                 cert['cert_digest'],
                 json.dumps(cert,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
    return cert

def latest_route_certificate(con, trip_id):
    row=con.execute('''SELECT raw_payload FROM route_certificates
                       WHERE trip_id=? ORDER BY version DESC LIMIT 1''',(trip_id,)).fetchone()
    return json.loads(row['raw_payload']) if row else None

def next_correction_seq(con, trip_id):
    row=con.execute('SELECT COUNT(*) n FROM route_corrections WHERE trip_id=?',(trip_id,)).fetchone()
    return int(row['n'])+1

def save_route_correction(con, correction):
    created_at=int(__import__('time').time())
    con.execute('INSERT INTO route_corrections VALUES(?,?,?,?,?,?,?,?)',
                (correction['correction_id'],correction['trip_id'],correction['kind'],
                 correction['prior_cert_digest'],correction['evidence_ref'],
                 correction['evidence_digest'],correction['reason'],created_at))
    return {**correction,'created_at':created_at}

def save_delivery_handover(con, row):
    con.execute('INSERT OR REPLACE INTO delivery_handovers VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (row['handover_id'],row['trip_id'],row['cert_id'],row['subject'],
                 row['box_id'],row['kind'],row.get('road'),int(row['observed_ts']),
                 row['state'],row['reason'],row['digest']))
    return row

def verified_handover(con, handover_id):
    row=con.execute('SELECT * FROM delivery_handovers WHERE id=?',(handover_id,)).fetchone()
    return dict(row) if row else None
