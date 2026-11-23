"""Per-instance pickup reservation ledger fed by ICS calendar imports.

Every VEVENT occurrence (base commitment, single-instance override,
single-instance cancel, whole-series cancel) is stored as its own
non-mergeable version in ``calendar_events``; the ``booking_instances``
table is a pure, deterministic function of those stored versions.  That
makes repeated imports, cancels arriving before the original series and
offline resume all converge to the same instance table.

Series are namespaced by ``source`` so identical UIDs from different
care institutions never overwrite each other.  Only ``confirmed``
instances are handed to ``service.prepare``/``service.pickup``.
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import storage as s
from . import calendar_feed as feed
from .calendar_feed import weekly_utc

STATUS_BY_CONVERSION = {'ok': 'confirmed', 'ambiguous': 'ambiguous',
                        'gap': 'gap', 'pending': 'pending'}
INSTANCE_COLS = ['id', 'series_id', 'source', 'uid', 'seq', 'rkey', 'domain',
                 'local', 'tzid', 'utc', 'window_end', 'candidates', 'fold',
                 'status', 'time_mode', 'attest_version', 'sequence', 'meal',
                 'rescheduled']


# --- legacy seed API (kept bit-compatible with tests/test_calendar_seed.py) --

def expand(item):
    return [{'booking_id':item['booking_id'],'starts_at':start} for start in weekly_utc(item['starts_at'],item['count'])]


# --- ingest ------------------------------------------------------------------

def _classify(ev, method):
    cancelled = ev['status'] == 'CANCELLED' or method == 'CANCEL'
    if ev['recurrence_id'] is None:
        return 'series-cancel' if cancelled else 'base'
    return 'cancel' if cancelled else 'override'


def _validate(ev, kind):
    if not ev['uid']:
        raise ValueError('missing-uid')
    if ev['time_mode'] not in (None, 'WALLCLOCK', 'UTC-INTERVAL'):
        raise ValueError('unknown-time-mode')
    if kind == 'base':
        if ev['dtstart'] is None:
            raise ValueError('missing-dtstart')
        # trial expansion pins down DTSTART/RRULE/mode coherence
        feed.expand_series(ev['dtstart'], ev['rrule'], ev['time_mode'], ev['display_tz'])
    elif kind == 'override' and ev['dtstart'] is None:
        raise ValueError('missing-dtstart')


def ingest_ics(con, text, source):
    """Parse and store every VEVENT version, then rebuild the ledger.

    The whole batch is validated before anything is stored, so a malformed
    stream never leaves a half-imported calendar behind.
    """
    parsed = feed.parse_ics(text)
    records = []
    for ev in parsed['events']:
        kind = _classify(ev, parsed['method'])
        _validate(ev, kind)
        records.append({'source': source, 'uid': ev['uid'],
                        'recurrence_key': feed.occurrence_key(ev['recurrence_id']) if ev['recurrence_id'] else None,
                        'kind': kind, 'sequence': ev['sequence'],
                        'dtstamp': ev['dtstamp'], 'payload': ev})
    stored = 0
    with con:
        for rec in records:
            if s.record_calendar(con, rec):
                stored += 1
        materialize(con)
    return {'source': source, 'seen': len(records), 'stored': stored}


# --- deterministic materialization -------------------------------------------

def materialize(con):
    """Rebuild booking_instances from all stored calendar versions."""
    rows = con.execute('SELECT * FROM calendar_events ORDER BY source,uid,sequence,dtstamp,digest').fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r['source'], r['uid']), []).append(r)
    con.execute('DELETE FROM booking_instances')
    for key in sorted(groups):
        _materialize_series(con, key[0], key[1], groups[key])


def _materialize_series(con, source, uid, versions):
    series_id = f'{source}:{uid}'
    base_versions = [v for v in versions if v['kind'] in ('base', 'series-cancel')]
    if not base_versions:
        return  # only cancels/overrides seen so far; they stay stored
    head = base_versions[-1]
    cancelled_whole = head['kind'] == 'series-cancel'
    if cancelled_whole:
        bases = [v for v in base_versions if v['kind'] == 'base']
        if not bases:
            return
        head = bases[-1]
    base = json.loads(head['payload'])
    key_versions = {}
    for v in versions:
        if v['kind'] in ('override', 'cancel'):
            key_versions.setdefault(v['recurrence_key'], []).append(v)
    pending = base['time_mode'] is None
    for occ in feed.expand_series(base['dtstart'], base['rrule'], base['time_mode'], base['display_tz']):
        actions = []
        if occ['rkey'] is not None:
            for ex in base['exdates']:
                if feed.occurrence_key(ex) == occ['rkey']:
                    actions.append((head['sequence'], head['dtstamp'], head['digest'], 'cancel', None))
            for v in key_versions.get(occ['rkey'], []):
                actions.append((v['sequence'], v['dtstamp'], v['digest'], v['kind'], v))
        row = {'id': f"{series_id}#{occ['rkey']}" if occ['rkey'] else f"{series_id}#pending-{occ['seq']}",
               'series_id': series_id, 'source': source, 'uid': uid, 'seq': occ['seq'],
               'rkey': occ['rkey'], 'domain': (occ['rkey'] or '').split('|')[0] or None,
               'local': occ['local'], 'tzid': occ['tzid'], 'utc': occ['utc'],
               'window_end': _window_end(occ['utc'], base['duration_minutes']),
               'candidates': json.dumps(occ['candidates'], ensure_ascii=False),
               'fold': occ['fold'], 'time_mode': base['time_mode'],
               'attest_version': base['attest_version'], 'sequence': head['sequence'],
               'meal': base['meal'], 'rescheduled': 0,
               'status': STATUS_BY_CONVERSION[occ['conversion']]}
        if cancelled_whole:
            row['status'] = 'cancelled'
        elif pending:
            row['status'] = 'pending'
        elif actions:
            # latest non-mergeable version wins: (sequence, dtstamp, digest)
            actions.sort(key=lambda a: (a[0], a[1] or '', a[2]))
            kind, payload = actions[-1][3], actions[-1][4]
            if kind == 'cancel':
                row['status'] = 'cancelled'
            else:
                _apply_override(row, json.loads(payload['payload']), base)
        _insert_instance(con, row)


def _apply_override(row, override, base):
    dt = override['dtstart']
    if dt['form'] == 'utc':
        instant = datetime.fromisoformat(dt['value'])
        utc_iso, fold, conv, cands = instant.isoformat(), None, 'ok', []
        tzid = override['display_tz'] or base['display_tz']
        if tzid:
            local = instant.astimezone(ZoneInfo(tzid))
            row['local'], row['tzid'], fold = local.replace(tzinfo=None).isoformat(), tzid, local.fold
        else:
            row['local'], row['tzid'] = None, None
    else:
        loc = feed.localize(dt['tzid'], dt['value'])
        utc_iso, fold, conv, cands = loc['utc'], loc['fold'], loc['conversion'], loc['candidates']
        row['local'], row['tzid'] = dt['value'], dt['tzid']
    minutes = override['duration_minutes'] or base['duration_minutes']
    row.update({'utc': utc_iso, 'window_end': _window_end(utc_iso, minutes),
                'fold': fold, 'candidates': json.dumps(cands, ensure_ascii=False),
                'status': STATUS_BY_CONVERSION[conv], 'rescheduled': 1,
                'attest_version': override['attest_version'] if override['attest_version'] is not None else base['attest_version'],
                'sequence': override['sequence'],
                'meal': override['meal'] or base['meal']})


def _window_end(utc_iso, minutes):
    if utc_iso is None or not minutes:
        return None
    return (datetime.fromisoformat(utc_iso) + timedelta(minutes=minutes)).isoformat()


def _insert_instance(con, row):
    con.execute(f"INSERT OR REPLACE INTO booking_instances({','.join(INSTANCE_COLS)}) VALUES({','.join('?' * len(INSTANCE_COLS))})",
                tuple(row[c] for c in INSTANCE_COLS))


# --- read side -----------------------------------------------------------------

def instances(con, source=None):
    q, args = 'SELECT * FROM booking_instances', ()
    if source:
        q, args = q + ' WHERE source=?', (source,)
    return [dict(r) for r in con.execute(q + ' ORDER BY id', args)]


def windows(con, source=None):
    """Confirmed, uncancelled pickup windows for service.prepare/pickup."""
    q, args = "SELECT * FROM booking_instances WHERE status='confirmed'", ()
    if source:
        q, args = q + ' AND source=?', (source,)
    return [dict(r) for r in con.execute(q + ' ORDER BY utc,id', args)]
